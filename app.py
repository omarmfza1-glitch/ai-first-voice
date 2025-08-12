# app.py — v12 (Twilio Media Streams + Google STT fix)
# ----------------------------------------------------
# أهم إصلاح هنا: أول رسالة إلى Google STT أصبحت StreamingRecognizeRequest(streaming_config=...)
# وبعدها فقط نرسل audio_content بالبايتات. هذا كان سبب “يرحب لكن لا يتفاعل”.
# كذلك أضفنا تتبع واضح لعدد الإطارات المرسلة إلى STT، ورسائل سجل تساعد في التشخيص.

import os, io, json, uuid, base64, queue, threading, logging, datetime
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

# Twilio (اختياري لتحديث الـTwiML بالتجاوب الصوتي)
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException

# OpenAI (للـLLM والـTTS)
from openai import OpenAI

# Google Cloud Speech (STT)
from google.cloud import speech_v1p1beta1 as speech

# ----------------------------------------------------
# الإعدادات العامة
# ----------------------------------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("smart-cc")

PORT = int(os.getenv("PORT", 5000))
BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}")  # IMPORTANT: ضع https://<app>.herokuapp.com في الإنتاج
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
GCP_KEY_JSON = os.getenv("GCP_KEY_JSON")

# اكتب مفاتيح GCP إن توفرت
if GCP_KEY_JSON:
    try:
        with open("gcp.json", "w", encoding="utf-8") as f:
            f.write(GCP_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
        logger.info("✅ GCP credentials file written.")
    except Exception as e:
        logger.error("Failed to write gcp.json: %s", e)

# عملاء الخدمات
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

twilio_client: Optional[TwilioClient] = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

try:
    speech_client = speech.SpeechClient()
    logger.info("✅ Google Cloud Speech Client initialized.")
except Exception as e:
    speech_client = None
    logger.error("❌ Failed to init Google Speech Client: %s", e)

# ----------------------------------------------------
# FastAPI + ملفات ثابتة
# ----------------------------------------------------
app = FastAPI()
os.makedirs("public/tts", exist_ok=True)
app.mount("/public", StaticFiles(directory="public"), name="public")

# ----------------------------------------------------
# حالة المكالمات
# ----------------------------------------------------
class CallState:
    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self.frames = 0
        self.stopped = False
        self.streamer: Optional["SpeechStreamer"] = None

CALLS: dict[str, CallState] = {}

# ----------------------------------------------------
# مولّد طلبات STT الصحيح: أول طلب = streaming_config، ثم الصوت فقط
# ----------------------------------------------------
class SpeechStreamer:
    def __init__(self, language_code: str = "ar-SA", alt_langs: Optional[list[str]] = None, sample_rate: int = 8000):
        self.q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.closed = False
        self.sample_rate = sample_rate
        self.language_code = language_code
        self.alt_langs = alt_langs or ["en-US"]
        self.thread: Optional[threading.Thread] = None

    def push(self, pcm: bytes):
        if not self.closed:
            self.q.put(pcm)

    def close(self):
        if not self.closed:
            self.closed = True
            self.q.put(None)

    def request_generator(self):
        # أول رسالة: الإعدادات
        yield speech.StreamingRecognizeRequest(
            streaming_config=speech.StreamingRecognitionConfig(
                config=speech.RecognitionConfig(
                    encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                    sample_rate_hertz=self.sample_rate,
                    language_code=self.language_code,
                    alternative_language_codes=self.alt_langs,
                    use_enhanced=True,
                    model="phone_call",
                    enable_automatic_punctuation=True,
                ),
                interim_results=True,
                single_utterance=False,
            )
        )
        # بقية الرسائل: صوت فقط
        while True:
            chunk = self.q.get()
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

# ----------------------------------------------------
# أدوات وهمية + LLM + TTS
# ----------------------------------------------------

def _mock_lookup_balance(customer_id: str):
    return {"customer_id": customer_id or "12345", "balance": 150.50}


def _mock_open_ticket(summary: str):
    return f"T-{str(uuid.uuid4())[:8]}"


async def llm_reply(user_text: str) -> str:
    if not openai_client:
        return "عذرًا، الخدمة غير متاحة مؤقتًا."
    system = "أنت مساعد اتصال لشركة اتصالات سعودية. أجب باختصار وبالعربية الفصحى المهذبة."
    try:
        comp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            temperature=0.2,
            max_tokens=180,
        )
        return (comp.choices[0].message.content or "").strip() or "أفهمك. كيف أستطيع مساعدتك؟"
    except Exception as e:
        logger.error("LLM error: %s", e)
        return "عذرًا، لم أفهم جيدًا. هل يمكنك التوضيح؟"


async def tts_url_from_text(text: str) -> Optional[str]:
    """يبني ملف MP3 محلي ويُرجع URL عام لكي تشغله Twilio عبر <Play>."""
    if not openai_client:
        return None
    file_id = f"{uuid.uuid4()}.mp3"
    out_path = os.path.join("public", "tts", file_id)
    url = f"{BASE_URL}/public/tts/{file_id}"
    try:
        with openai_client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text,
            response_format="mp3",
        ) as resp:
            resp.stream_to_file(out_path)
        logger.info("🔊 TTS saved -> %s", url)
        return url
    except TypeError:
        # fallback لإصدارات لا تدعم response_format
        with openai_client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text,
        ) as resp:
            resp.stream_to_file(out_path)
        logger.info("🔊 TTS saved (fallback) -> %s", url)
        return url
    except Exception as e:
        logger.error("TTS error: %s", e)
        return None


async def handle_user_turn(call_sid: str, transcript: str):
    logger.info("🗣️ STT FINAL [%s]: %s", call_sid, transcript)
    reply = await llm_reply(transcript)

    mp3 = await tts_url_from_text(reply)
    if not twilio_client:
        return
    try:
        if mp3:
            twiml = f"""
<Response>
  <Play>{mp3}</Play>
  <Redirect method=\"POST\">/twilio/voice</Redirect>
</Response>
""".strip()
        else:
            twiml = f"""
<Response>
  <Say language=\"ar-SA\" voice=\"Polly.Zeina\">{reply}</Say>
  <Redirect method=\"POST\">/twilio/voice</Redirect>
</Response>
""".strip()
        twilio_client.calls(call_sid).update(twiml=twiml)
        logger.info("📞 Twilio call updated with reply (mp3=%s)", bool(mp3))
    except TwilioException as e:
        logger.error("Twilio update error [%s]: %s", call_sid, e)


# ----------------------------------------------------
# مسارات Twilio: Voice (TwiML) + Media (WebSocket) + Status
# ----------------------------------------------------
@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")

    # بناء مسار الـWebSocket (wss)
    from urllib.parse import urlparse
    ws_host = urlparse(BASE_URL).netloc or request.url.netloc
    wss_url = f"wss://{ws_host}/twilio/media"

    # حيّز الحالة
    if call_sid not in CALLS:
        CALLS[call_sid] = CallState(call_sid)

    # تحية + بدء البث
    twiml = f"""
<Response>
  <Start>
    <Stream url=\"{wss_url}\" track=\"inbound_track\"/>
  </Start>
  <Say language=\"ar-SA\" voice=\"Polly.Zeina\">مرحبًا بك في مركز الاتصال الذكي. تفضّل بالتحدث، أنا أستمع.</Say>
  <Pause length=\"60\"/>
</Response>
""".strip()
    logger.info("📞 Greeting handler: Welcoming call %s and redirecting to stream.", call_sid)
    return Response(content=twiml, media_type="text/xml; charset=utf-8")


@app.websocket("/twilio/media")
async def twilio_media(ws: WebSocket):
    await ws.accept()
    logger.info("connection open")

    # سنستخرج callSid من حدث start
    current_call_sid = None

    # مُهيّئ الـSTT
    streamer: Optional[SpeechStreamer] = None

    try:
        while True:
            msg = await ws.receive_text()
            event = json.loads(msg)
            etype = event.get("event")

            if etype == "start":
                start_info = event.get("start", {})
                current_call_sid = start_info.get("callSid") or start_info.get("streamSid") or ""
                logger.info("▶️ WS Receiver: Stream started for call: %s", current_call_sid)

                # أنشئ الـstreamer وابدأ خيط قراءة نتائج STT
                streamer = SpeechStreamer(language_code="ar-SA", alt_langs=["en-US"], sample_rate=8000)

                def stt_reader():
                    if not speech_client:
                        return
                    try:
                        responses = speech_client.streaming_recognize(streamer.request_generator())
                        for resp in responses:
                            for result in resp.results:
                                if not result.alternatives:
                                    continue
                                alt = result.alternatives[0]
                                transcript = (alt.transcript or "").strip()
                                if result.is_final and transcript:
                                    # نفّذ ردّ التطبيق في مسار الحدث الرئيسي
                                    import asyncio
                                    loop = asyncio.get_event_loop()
                                    loop.call_soon_threadsafe(asyncio.create_task, handle_user_turn(current_call_sid, transcript))
                    except Exception as e:
                        logger.error("STT stream error: %s", e)

                t = threading.Thread(target=stt_reader, daemon=True)
                t.start()

                # خزّن الحالة
                if current_call_sid and current_call_sid not in CALLS:
                    CALLS[current_call_sid] = CallState(current_call_sid)

            elif etype == "media":
                if not streamer:
                    continue
                payload = event.get("media", {}).get("payload")
                if not payload:
                    continue
                try:
                    ulaw = base64.b64decode(payload)
                    # PCMU (mu-law 8k) -> Linear16 (16-bit)
                    # audioop.ulaw2lin يتطلب عرض عينة 2 = 16-bit
                    import audioop
                    pcm = audioop.ulaw2lin(ulaw, 2)
                    streamer.push(pcm)

                    # عدّاد الإطارات
                    if current_call_sid:
                        st = CALLS.get(current_call_sid)
                        if st:
                            st.frames += 1
                            if st.frames == 1:
                                logger.info("🎙️ First media frame received for %s (frames so far: 1)", current_call_sid)
                            elif st.frames % 100 == 0:
                                logger.info("🎙️ Media frames forwarded to STT for %s: %d", current_call_sid, st.frames)
                except Exception as e:
                    logger.error("Media decode error: %s", e)

            elif etype == "mark":
                # اختياري: إشارات وقتية من Twilio
                pass

            elif etype == "stop":
                logger.info("⏹️ WS Receiver: Stream stopped. Ending queue.")
                if streamer:
                    streamer.close()
                break

    except WebSocketDisconnect:
        logger.info("WS disconnect")
    except Exception as e:
        logger.error("WS error: %s", e)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
        logger.info("connection closed")


@app.post("/twilio/status")
async def twilio_status(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    status = form.get("CallStatus", "")
    logger.info("📞 Call Status: %s -> %s", call_sid, status)
    if call_sid and status in {"completed", "canceled", "failed", "busy", "no-answer"}:
        if call_sid in CALLS:
            del CALLS[call_sid]
            logger.info("🧹 Cleaned up state for call %s", call_sid)
    return Response(status_code=200)


@app.get("/health")
async def health():
    return PlainTextResponse("OK")


if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 Starting Smart Call Center...")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
