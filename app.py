import os
import io
import base64
import uuid
import sqlite3
import datetime
import json
import asyncio
import logging
import queue
import threading
from typing import Optional, Iterator

# محاولة دعم audioop على بايثون 3.13
try:
    import audioop  # Python <= 3.12
except ModuleNotFoundError:  # pragma: no cover
    import audioop_lts as audioop  # type: ignore

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

# Twilio / OpenAI / Google STT
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException
from openai import OpenAI
from google.cloud import speech_v1p1beta1 as speech
from google.cloud.speech_v1p1beta1 import (
    StreamingRecognizeRequest,
    StreamingRecognitionConfig,
    RecognitionConfig,
)

# ──────────────────────────────────────────────── إعدادات عامة
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("smart-cc")

GCP_KEY_JSON = os.getenv("GCP_KEY_JSON")
if GCP_KEY_JSON:
    try:
        with open("gcp.json", "w", encoding="utf-8") as f:
            f.write(GCP_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
        logger.info("✅ GCP credentials written to gcp.json")
    except Exception as e:
        logger.error("Failed to write gcp.json: %s", e)

PORT = int(os.getenv("PORT", 5000))
BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
speech_client = speech.SpeechClient()
logger.info("✅ Google Cloud Speech Client initialized")

twilio_client = (
    TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)
    else None
)

# ──────────────────────────────────────────────── قاعدة بيانات خفيفة
DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
with sqlite3.connect(DB_PATH) as _conn:
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            call_sid TEXT,
            turn INTEGER,
            user_text TEXT,
            intent TEXT,
            tool_called TEXT,
            tool_result TEXT,
            reply_text TEXT,
            reply_audio_url TEXT
        );
        """
    )
logger.info("✅ Database initialized successfully.")

# ──────────────────────────────────────────────── FastAPI
app = FastAPI()
os.makedirs("public/tts", exist_ok=True)
app.mount("/public", StaticFiles(directory="public"), name="public")

# حالة المكالمة بالذاكرة
CALL_STATE: dict[str, dict] = {}


def log_conv(call_sid: str, turn: int, user_text: str, intent: str, tool_called: str, tool_result: str, reply_text: str, reply_audio_url: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO conversations (timestamp, call_sid, turn, user_text, intent, tool_called, tool_result, reply_text, reply_audio_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.datetime.utcnow().isoformat(),
                call_sid,
                turn,
                user_text,
                intent or "",
                tool_called or "",
                tool_result or "",
                reply_text or "",
                reply_audio_url or "",
            ),
        )


# ──────────────────────────────────────────────── TwiML: ترحيب ثم بدء الستريم
@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    logger.info("📞 Greeting handler: Welcoming call %s and redirecting to stream.", call_sid)

    # نستخدم Redirect لبدء الستريم في خطوة منفصلة (أكثر استقراراً مع بعض حسابات Twilio)
    twiml = (
        """
<Response>
  <Say language="ar-SA" voice="Polly.Zeina">مرحبًا بك في مركز الاتصال الذكي. تفضل بالحديث بعد الصافرة.</Say>
  <Redirect method="POST">/twilio/stream</Redirect>
</Response>
"""
    ).strip()
    return Response(content=twiml, media_type="text/xml; charset=utf-8")


@app.post("/twilio/stream")
async def twilio_stream(request: Request):
    # نبني عنوان الـ WebSocket من طلب Twilio نفسه لضمان نفس الدومين/البروتوكول
    ws_url = f"wss://{request.url.netloc}/twilio/media"
    logger.info("🎧 Stream handler: Starting media stream for call %s", (await request.form()).get("CallSid", ""))
    twiml = f"""
<Response>
  <Start>
    <Stream url="{ws_url}"/>
  </Start>
  <Pause length="60"/>
</Response>
""".strip()
    return Response(content=twiml, media_type="text/xml; charset=utf-8")


# ──────────────────────────────────────────────── WebSocket Media Stream
class AudioQueue:
    def __init__(self):
        self.q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.closed = False

    def push(self, pcm: bytes):
        if not self.closed:
            self.q.put(pcm)

    def close(self):
        if not self.closed:
            self.closed = True
            self.q.put(None)

    def __iter__(self) -> Iterator[Optional[bytes]]:
        while True:
            item = self.q.get()
            if item is None:
                break
            yield item


def make_stt_requests_iter(audio_iter: Iterator[Optional[bytes]]):
    # أول رسالة: Streaming config (لا نستخدم alternative_language_codes لأن الموديل phone_call لا يدعمها)
    streaming_config = StreamingRecognitionConfig(
        config=RecognitionConfig(
            encoding=RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,  # Twilio 8kHz
            language_code="ar-SA",
            use_enhanced=True,
            model="phone_call",
            enable_automatic_punctuation=True,
        ),
        interim_results=True,
        single_utterance=False,
    )
    yield StreamingRecognizeRequest(streaming_config=streaming_config)

    # بقية الرسائل: الصوت
    for pcm in audio_iter:
        if pcm:
            yield StreamingRecognizeRequest(audio_content=pcm)


async def _handle_user_turn(call_sid: str, user_text: str):
    """ينتج رد، يحوله إلى صوت، ثم يطلب من Twilio تشغيله داخل نفس المكالمة."""
    reply_text = f"سمعتك تقول: {user_text}. كيف أستطيع مساعدتك؟"

    # يمكن استبدال المنطق أعلاه بنداء OpenAI Chat (اختياري)
    if openai_client:
        try:
            comp = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "أنت وكيل خدمة عملاء. أجب بجمل قصيرة مهذبة بالعربية."},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.2,
                max_tokens=120,
            )
            reply_text = comp.choices[0].message.content.strip() or reply_text
        except Exception as e:
            logger.error("LLM error: %s", e)

    mp3_url = await _synthesize_tts(reply_text)

    if twilio_client:
        try:
            if mp3_url:
                twiml = f"""
<Response>
  <Play>{mp3_url}</Play>
  <Redirect method=\"POST\">/twilio/stream</Redirect>
</Response>
""".strip()
            else:
                twiml = f"""
<Response>
  <Say language=\"ar-SA\" voice=\"Polly.Zeina\">{reply_text}</Say>
  <Redirect method=\"POST\">/twilio/stream</Redirect>
</Response>
""".strip()
            twilio_client.calls(call_sid).update(twiml=twiml)
            logger.info("📣 Sent reply to call %s (mp3=%s)", call_sid, bool(mp3_url))
        except TwilioException as e:
            logger.error("Twilio update error [%s]: %s", call_sid, e)

    st = CALL_STATE.get(call_sid, {"turn": 0})
    st["turn"] = st.get("turn", 0) + 1
    CALL_STATE[call_sid] = st
    log_conv(call_sid, st["turn"], user_text, "", "", "", reply_text, mp3_url or "")


async def _synthesize_tts(text: str) -> Optional[str]:
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
        return url
    except TypeError:
        # لبعض الإصدارات القديمة التي لا تدعم response_format
        with openai_client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text,
        ) as resp:
            resp.stream_to_file(out_path)
        return url
    except Exception as e:
        logger.error("TTS error: %s", e)
        return None


@app.websocket("/twilio/media")
async def media_ws(ws: WebSocket):
    await ws.accept()
    logger.info("connection open")

    loop = asyncio.get_event_loop()
    audio_q = AudioQueue()
    call_sid_holder = {"call_sid": ""}
    frames = 0

    # مستهلك STT يعمل في خيط مستقل (blocking gRPC)
    def stt_worker():
        try:
            for response in speech_client.streaming_recognize(requests=make_stt_requests_iter(iter(audio_q))):
                for result in response.results:
                    if not result.alternatives:
                        continue
                    transcript = (result.alternatives[0].transcript or "").strip()
                    if not transcript:
                        continue
                    if result.is_final:
                        logger.info("📝 STT FINAL [%s]: %s", call_sid_holder["call_sid"], transcript)
                        asyncio.run_coroutine_threadsafe(
                            _handle_user_turn(call_sid_holder["call_sid"], transcript),
                            loop,
                        )
        except Exception as e:
            logger.error("❌ STT responses loop error: %s", e)

    t = threading.Thread(target=stt_worker, daemon=True)
    t.start()

    try:
        while True:
            raw = await ws.receive_text()
            event = json.loads(raw)
            etype = event.get("event")

            if etype == "start":
                start_info = event.get("start", {})
                call_sid_holder["call_sid"] = start_info.get("callSid", "")
                logger.info("▶️ WS Receiver: Stream started for call: %s", call_sid_holder["call_sid"])

            elif etype == "media":
                payload_b64 = event.get("media", {}).get("payload")
                if payload_b64:
                    ulaw = base64.b64decode(payload_b64)
                    pcm16 = audioop.ulaw2lin(ulaw, 2)  # 16-bit PCM
                    audio_q.push(pcm16)
                    frames += 1
                    if frames % 100 == 0:
                        logger.info("🎙️ Media frames forwarded to STT for %s: %d", call_sid_holder["call_sid"], frames)

            elif etype == "stop":
                logger.info("⏹️ WS Receiver: Stream stopped. Ending queue.")
                break

    except WebSocketDisconnect:
        logger.info("WS disconnect")
    except Exception as e:
        logger.exception("WS error: %s", e)
    finally:
        audio_q.close()
        try:
            t.join(timeout=2)
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
        logger.info("connection closed")


# إشعار حالة المكالمة من Twilio (اختياري)
@app.post("/twilio/status")
async def twilio_status(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    logger.info("📞 Call Status: %s -> %s", call_sid, call_status)
    if call_status in {"completed", "canceled", "failed", "busy", "no-answer"}:
        CALL_STATE.pop(call_sid, None)
        logger.info("🧹 Cleaned up state for call %s", call_sid)
    return Response(status_code=200)


@app.get("/health")
async def health():
    return PlainTextResponse("OK")


if __name__ == "__main__":
    import uvicorn

    logger.info("🚀 Starting Smart Call Center...")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
