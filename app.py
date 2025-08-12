# app.py — Smart Call Center (Twilio Media Streams + Google STT)
# إصلاح جذري: تمرير requests بشكل صحيح إلى streaming_recognize
# وحقن أول رسالة StreamingRecognizeRequest(streaming_config=...) داخل المولّد.

import os, json, base64, asyncio, logging, uuid, queue, threading
from typing import Optional

# الصوت: دعم μ-law -> PCM16
try:
    import audioop  # Python <= 3.12
except ModuleNotFoundError:
    import audioop_lts as audioop  # بايثون 3.13

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

from google.cloud import speech_v1p1beta1 as speech
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException

# (اختياري) LLM/TTS — آمنين بالفشل
try:
    from openai import OpenAI
except Exception:  # pragma: no cover
    OpenAI = None  # لتجنّب الأعطال لو الحزمة غير متوفرة

load_dotenv()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger("smart-cc")

# --- البيئة ---
PORT = int(os.getenv("PORT", 5000))
BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
GCP_KEY_JSON = os.getenv("GCP_KEY_JSON")

if GCP_KEY_JSON:
    try:
        with open("gcp.json", "w", encoding="utf-8") as f:
            f.write(GCP_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
        log.info("✅ GCP credentials written to gcp.json")
    except Exception as e:
        log.error("❌ Failed to write gcp.json: %s", e)

# عملاء خارجيون
speech_client = speech.SpeechClient()  # متزامن
log.info("✅ Google Cloud Speech Client initialized")

twilio_client: Optional[TwilioClient] = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

openai_client = None
if OPENAI_API_KEY and OpenAI:
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        openai_client = None

# --- تطبيق FastAPI ---
app = FastAPI()
os.makedirs("public/tts", exist_ok=True)
app.mount("/public", StaticFiles(directory="public"), name="public")

# حالة المكالمة
class CallState:
    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self.frames = 0
        self.req_iter: Optional["StreamingRequests"] = None
        self.stt_thread: Optional[threading.Thread] = None
        self.running = False

CALLS: dict[str, CallState] = {}

# مولّد requests للـ STT — يرسل config أولاً ثم الصوت
class StreamingRequests:
    def __init__(self, streaming_config: speech.StreamingRecognitionConfig, include_config_first: bool = True):
        self.streaming_config = streaming_config
        self.include_config_first = include_config_first
        self.q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.closed = False
        self._first_sent = False

    def push(self, pcm16: bytes):
        if not self.closed:
            self.q.put(pcm16)

    def close(self):
        if not self.closed:
            self.closed = True
            self.q.put(None)

    def __iter__(self):
        # حقن رسالة الإعداد أولاً عند الحاجة
        if self.include_config_first and not self._first_sent:
            self._first_sent = True
            yield speech.StreamingRecognizeRequest(streaming_config=self.streaming_config)
        while True:
            chunk = self.q.get()
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

# --- منافع ---
async def _twilio_say(call_sid: str, text: str):
    if not twilio_client:
        return
    try:
        twiml = f"""
<Response>
  <Say language=\"ar-SA\" voice=\"Polly.Zeina\">{text}</Say>
  <Redirect method=\"POST\">/twilio/voice</Redirect>
</Response>
""".strip()
        twilio_client.calls(call_sid).update(twiml=twiml)
        log.info("📣 Spoke via <Say> to %s", call_sid)
    except TwilioException as e:
        log.error("❌ Twilio update error [%s]: %s", call_sid, e)

async def _on_final_transcript(call_sid: str, text: str):
    log.info("📝 FINAL [%s]: %s", call_sid, text)
    reply = await _plan_and_reply(text)
    await _twilio_say(call_sid, reply)

async def _plan_and_reply(utterance: str) -> str:
    # من أجل البساطة الآن — رد مهذب موجز
    reply = None
    if openai_client:
        try:
            comp = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "أنت وكيل خدمة عملاء. أجب باختصار ومباشرةً بالعربية الفصحى."},
                    {"role": "user", "content": utterance},
                ],
                temperature=0.3,
                max_tokens=120,
            )
            reply = comp.choices[0].message.content.strip()
        except Exception as e:
            log.warning("LLM fallback due to: %s", e)
    if not reply:
        reply = f"سمعتك تقول: {utterance}. كيف يمكنني مساعدتك؟"
    return reply

# تشغيل حلقة STT (خيط مستقل لمنع حظر asyncio)
def _run_stt_loop(call_sid: str, responses, loop: asyncio.AbstractEventLoop):
    try:
        for resp in responses:
            for result in resp.results:
                if result.is_final and result.alternatives:
                    transcript = result.alternatives[0].transcript.strip()
                    if transcript:
                        asyncio.run_coroutine_threadsafe(_on_final_transcript(call_sid, transcript), loop)
    except Exception as e:
        log.error("❌ STT responses loop error: %s", e)

# --- مسارات Twilio ---
@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")

    from urllib.parse import urlparse
    host = urlparse(BASE_URL).netloc or request.url.netloc
    wss = f"wss://{host}/twilio/media?callSid={call_sid}"

    twiml = f"""
<Response>
  <Start>
    <Stream url=\"{wss}\" />
  </Start>
  <Say language=\"ar-SA\" voice=\"Polly.Zeina\">مرحبًا بك في مركز الاتصال الذكي. تفضل بالتحدث، أنا أستمع.</Say>
  <Pause length=\"60\"/>
</Response>
""".strip()

    log.info("📞 Greeting handler: Welcoming call %s and redirecting to stream.", call_sid)
    return Response(content=twiml, media_type="text/xml; charset=utf-8")

@app.post("/twilio/status")
async def twilio_status(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    log.info("📞 Call Status: %s -> %s", call_sid, call_status)
    # تنظيف الحالة عند اكتمال المكالمة
    if call_sid and call_status in {"completed", "canceled", "failed", "busy", "no-answer"}:
        CALLS.pop(call_sid, None)
        log.info("🧹 Cleaned up state for call %s", call_sid)
    return Response(status_code=200)

@app.websocket("/twilio/media")
async def twilio_media(ws: WebSocket):
    await ws.accept()
    query = ws.scope.get("query_string", b"").decode()
    call_sid = ""
    if "callSid=" in query:
        call_sid = query.split("callSid=")[1].split("&")[0]
    log.info("connection open")

    state = CALLS.get(call_sid) or CallState(call_sid)
    CALLS[call_sid] = state

    # إعداد Google STT config (8kHz, phone_call, ar-SA)
    streaming_config = speech.StreamingRecognitionConfig(
        config=speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="ar-SA",
            alternative_language_codes=["en-US"],
            use_enhanced=True,
            model="phone_call",
            enable_automatic_punctuation=True,
        ),
        interim_results=True,
        single_utterance=False,
    )

    # تحضير المولّد وتمريره بالطريقة الصحيحة وفق إصدار المكتبة
    include_config_first = True
    req_iter = StreamingRequests(streaming_config, include_config_first=include_config_first)

    # ابدأ التدفق إلى واجهة Google Speech
    try:
        try:
            # التوقيع الجديد: streaming_recognize(requests=iterable)
            responses = speech_client.streaming_recognize(req_iter)
        except TypeError:
            # بعض الإصدارات القديمة تطلب (streaming_config, requests)
            req_iter = StreamingRequests(streaming_config, include_config_first=False)
            responses = speech_client.streaming_recognize(streaming_config, req_iter)
        state.req_iter = req_iter
        state.running = True
        loop = asyncio.get_running_loop()
        state.stt_thread = threading.Thread(target=_run_stt_loop, args=(call_sid, responses, loop), daemon=True)
        state.stt_thread.start()
        log.info("▶️ WS Receiver: Stream started for call: %s", call_sid)
    except Exception as e:
        log.error("❌ STT stream error: %s", e)

    try:
        first_logged = False
        while True:
            msg = await ws.receive_text()
            event = json.loads(msg)
            etype = event.get("event")
            if etype == "start":
                # لا شيء إضافي — نحن بالفعل قمنا بتشغيل STT
                pass
            elif etype == "media":
                payload_b64 = event.get("media", {}).get("payload")
                if payload_b64 and state.req_iter and state.running:
                    ulaw = base64.b64decode(payload_b64)
                    pcm16 = audioop.ulaw2lin(ulaw, 2)
                    state.req_iter.push(pcm16)
                    state.frames += 1
                    if not first_logged:
                        log.info("🎙️ First media frame received for %s (frames so far: %d)", call_sid, state.frames)
                        first_logged = True
                    if state.frames % 100 == 0:
                        log.info("🎙️ Media frames forwarded to STT for %s: %d", call_sid, state.frames)
            elif etype == "stop":
                log.info("⏹️ WS Receiver: Stream stopped. Ending queue.")
                break
    except WebSocketDisconnect:
        log.info("🔌 WS disconnect [%s]", call_sid)
    except Exception as e:
        log.error("❌ WS error [%s]: %s", call_sid, e)
    finally:
        try:
            if state.req_iter:
                state.req_iter.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
        log.info("connection closed")

@app.get("/health")
async def health():
    return PlainTextResponse("OK")

if __name__ == "__main__":
    import uvicorn
    log.info("🚀 Starting Smart Call Center...")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
