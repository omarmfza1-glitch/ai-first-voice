# app.py — Smart Call Center (v11)
# -*- coding: utf-8 -*-
"""
تحسينات هذا الإصدار v11:
1) إصلاح إعداد Google Streaming Recognize: أول رسالة يجب أن تكون
   StreamingRecognizeRequest(streaming_config=...).
2) تسجيلات أوضح لتتبّع وسائط Twilio: عداد إطارات media مع أول وصول للصوت.
3) تأكيد استخدام wss وتحديد track="inbound_track" في TwiML.
4) إبقاء البنية كما هي مع FastAPI + Twilio + OpenAI TTS.
"""

# ============================================================================
# Imports
# ============================================================================
import os, base64, uuid, sqlite3, datetime, json, asyncio, logging, re
from typing import Optional, Dict, Any, AsyncIterator
from contextlib import asynccontextmanager

try:
    import audioop  # Python <=3.12
except ModuleNotFoundError:
    import audioop_lts as audioop  # Python 3.13

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# Logging
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("smart-cc")

# ============================================================================
# Env
# ============================================================================
PORT = int(os.getenv("PORT", 8000))
BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"

# Google creds (optional)
GCP_KEY_JSON_STR = os.getenv("GCP_KEY_JSON")
if GCP_KEY_JSON_STR:
    try:
        with open("gcp.json", "w", encoding="utf-8") as f:
            f.write(GCP_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
    except Exception as e:
        logger.warning(f"⚠️ Could not write gcp.json: {e}")

# ============================================================================
# External services
# ============================================================================
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException
from openai import OpenAI

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
if not openai_client:
    logger.warning("⚠️ OpenAI API key not configured.")

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None
if not twilio_client:
    logger.warning("⚠️ Twilio client not initialized.")

# Google STT (async client)
try:
    from google.cloud.speech_v1p1beta1.services import speech
    from google.cloud.speech_v1p1beta1 import types as speech_types
    speech_async_client = speech.SpeechAsyncClient()
    logger.info("✅ Google Cloud Speech Async Client initialized successfully.")
except Exception as e:
    speech_async_client = None
    speech_types = None
    logger.warning(f"⚠️ Google STT not available: {e}")

# ============================================================================
# DB
# ============================================================================
DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")

def init_database():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
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
            reply_audio_url TEXT,
            duration_ms INTEGER
        )
        """
    )
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized successfully.")

# ============================================================================
# FastAPI app
# ============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Starting Smart Call Center...")
    init_database()
    yield
    logger.info("👋 Shutting down Smart Call Center...")

app = FastAPI(title="Smart Call Center API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

os.makedirs("public/tts", exist_ok=True)
app.mount("/public", StaticFiles(directory="public"), name="public")

# Simple in-memory state
CALL_STATE: Dict[str, Dict[str, Any]] = {}

# ============================================================================
# Helpers
# ============================================================================

def log_conversation(**kwargs):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO conversations (timestamp, call_sid, turn, user_text, intent, tool_called, tool_result, reply_text, reply_audio_url, duration_ms)
            VALUES (:timestamp, :call_sid, :turn, :user_text, :intent, :tool_called, :tool_result, :reply_text, :reply_audio_url, :duration_ms)
            """,
            kwargs,
        )
        conn.commit(); conn.close()
    except Exception as e:
        logger.error(f"Failed to log conversation: {e}")

# Convert http/https -> ws/wss
_def_ws_url = lambda url: re.sub(r"^http", "ws", url)

# ============================================================================
# Twilio webhooks
# ============================================================================
@app.post("/twilio/voice")
async def voice_handler(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", f"local_{uuid.uuid4()}")
    from_number = form.get("From", "unknown")
    CALL_STATE.setdefault(call_sid, {"turn": 0, "from_number": from_number})

    # Greeting, then redirect to stream
    ws_url = _def_ws_url(BASE_URL) + "/twilio/media"
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="ar-SA" voice="Polly.Zeina">مرحبًا بكم في مركز الاتصال الذكي. تفضّل بالحديث بعد الصافرة.</Say>
  <Start>
    <Stream url="{ws_url}" track="inbound_track">
      <Parameter name="callSid" value="{call_sid}"/>
    </Stream>
  </Start>
  <Pause length="60"/>
</Response>
""".strip()
    logger.info(f"📞 Greeting handler: Welcoming call {call_sid} and redirecting to stream.")
    return Response(content=twiml, media_type="text/xml; charset=utf-8")

@app.websocket("/twilio/media")
async def media_stream_handler(ws: WebSocket):
    await ws.accept()
    call_sid: Optional[str] = None
    audio_q: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
    media_frames = 0
    got_first_audio = False

    async def receiver() -> None:
        nonlocal call_sid, media_frames, got_first_audio
        try:
            while True:
                payload = await ws.receive_json()
                event = payload.get("event")
                if event == "start":
                    start = payload.get("start", {})
                    call_sid = start.get("customParameters", {}).get("callSid") or start.get("callSid")
                    logger.info(f"▶️ WS Receiver: Stream started for call: {call_sid}")
                elif event == "media":
                    media_frames += 1
                    if not got_first_audio:
                        got_first_audio = True
                        logger.info(f"🎙️ First media frame received for {call_sid} (frames so far: {media_frames})")
                    ulaw = base64.b64decode(payload["media"]["payload"])  # 8kHz μ-law
                    pcm16 = audioop.ulaw2lin(ulaw, 2)  # -> 16-bit linear PCM
                    await audio_q.put(pcm16)
                elif event == "stop":
                    logger.info("⏹️ WS Receiver: Stream stopped. Ending queue.")
                    await audio_q.put(None)
                    break
                # (Twilio may also send mark/ping events — ignore.)
        except WebSocketDisconnect:
            logger.info("WS disconnected by client (Twilio).")
            await audio_q.put(None)
        except Exception as e:
            logger.error(f"WS receiver error: {e}")
            await audio_q.put(None)

    async def stt_requests_gen() -> AsyncIterator:
        """Async generator for Google STT requests (CORRECT FIRST MESSAGE)."""
        if not speech_types:
            return
        # FIRST: send streaming_config wrapped in StreamingRecognizeRequest
        streaming_config = speech_types.StreamingRecognitionConfig(
            config=speech_types.RecognitionConfig(
                encoding=speech_types.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=8000,
                language_code="ar-SA",
                model="phone_call",
                use_enhanced=True,
                enable_automatic_punctuation=True,
            ),
            interim_results=False,
            single_utterance=False,
        )
        yield speech_types.StreamingRecognizeRequest(streaming_config=streaming_config)
        # THEN: audio chunks
        while True:
            chunk = await audio_q.get()
            if chunk is None:
                break
            yield speech_types.StreamingRecognizeRequest(audio_content=chunk)

    async def stt_consumer() -> None:
        if not (speech_async_client and speech_types):
            logger.warning("⚠️ STT unavailable — no interaction will happen.")
            # Still keep WS alive until Twilio stops
            return
        try:
            responses = await speech_async_client.streaming_recognize(requests=stt_requests_gen())
            async for response in responses:
                if not response.results:
                    continue
                result = response.results[0]
                if not result.alternatives:
                    continue
                transcript = result.alternatives[0].transcript.strip()
                if transcript:
                    logger.info(f"🎤 STT Final [{call_sid}]: '{transcript}'")
                    asyncio.create_task(_handle_user_turn(call_sid, transcript))
        except Exception as e:
            logger.error(f"STT streaming error: {e}")

    recv_task = asyncio.create_task(receiver())
    stt_task = asyncio.create_task(stt_consumer())

    await recv_task
    # ensure stt_task finishes
    try:
        await stt_task
    except Exception:
        pass
    logger.info(f"🔚 WebSocket cleanup for call {call_sid} completed. Media frames: {media_frames}")

@app.post("/twilio/status")
async def status_callback_handler(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid")
    call_status = form.get("CallStatus")
    logger.info(f"📞 Call Status: {call_sid} -> {call_status}")
    if call_status in {"completed", "failed", "no-answer", "canceled", "busy"}:
        CALL_STATE.pop(call_sid, None)
        logger.info(f"🧹 Cleaned up state for call {call_sid}")
    return PlainTextResponse("")

# ============================================================================
# AI logic (LLM + TTS)
# ============================================================================
async def _handle_user_turn(call_sid: Optional[str], user_text: str) -> None:
    if not call_sid:
        return
    start = datetime.datetime.utcnow()
    state = CALL_STATE.setdefault(call_sid, {"turn": 0})

    # Very small planner — keep it simple
    reply_text = await _llm_reply(user_text, state)
    final_text = reply_text or "عذرًا، لم أفهم سؤالك."  # fallback
    mp3_url = await _synthesize_tts(final_text)

    if twilio_client and not call_sid.startswith("local_"):
        try:
            if mp3_url:
                body = f"""
<Response>
  <Play>{mp3_url}</Play>
  <Redirect method=\"POST\">{BASE_URL}/twilio/voice</Redirect>
</Response>""".strip()
            else:
                body = f"""
<Response>
  <Say language=\"ar-SA\" voice=\"Polly.Zeina\">{final_text}</Say>
  <Redirect method=\"POST\">{BASE_URL}/twilio/voice</Redirect>
</Response>""".strip()
            twilio_client.calls(call_sid).update(twiml=body)
            logger.info(f"✅ Call updated for {call_sid} (replied)")
        except TwilioException as e:
            logger.error(f"Twilio update error for {call_sid}: {e}")

    state["turn"] = state.get("turn", 0) + 1
    duration_ms = int((datetime.datetime.utcnow() - start).total_seconds() * 1000)
    log_conversation(
        timestamp=start.isoformat(), call_sid=call_sid, turn=state["turn"],
        user_text=user_text, intent="auto", tool_called=None, tool_result=None,
        reply_text=final_text, reply_audio_url=mp3_url or "", duration_ms=duration_ms,
    )

async def _llm_reply(user_text: str, state: dict) -> str:
    if not openai_client:
        return "الخدمة غير متاحة الآن."
    try:
        system = "أنت مساعد صوتي عربي مهذب لشركة اتصالات سعودية. أجب بإيجاز وبوضوح."
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            temperature=0.2,
            max_tokens=180,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error(f"LLM error: {e}")
        return "عذرًا، حدث خلل مؤقت."

async def _synthesize_tts(text: str) -> Optional[str]:
    if not (openai_client and text):
        return None
    try:
        file_id = f"{uuid.uuid4()}.mp3"
        out_path = os.path.join("public", "tts", file_id)
        # OpenAI TTS — modern endpoint
        audio = openai_client.audio.speech.create(model="tts-1", voice="alloy", input=text)
        audio.stream_to_file(out_path)
        url = f"{BASE_URL}/public/tts/{file_id}"
        logger.info(f"🔊 TTS ready: {url}")
        return url
    except Exception as e:
        logger.error(f"TTS error: {e}")
        return None

# ============================================================================
# Admin endpoints
# ============================================================================
@app.get("/")
async def root():
    return {"message": "Smart Call Center API is running."}

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "services": {
            "database": "ok" if os.path.exists(DB_PATH) else "missing",
            "openai": "ok" if openai_client else "disabled",
            "twilio": "ok" if twilio_client else "disabled",
            "google_stt": "ok" if speech_async_client else "disabled",
        },
    }

# ============================================================================
# Run (local)
# ============================================================================
if __name__ == "__main__":
    import uvicorn
    logger.info("=" * 60)
    logger.info(f"🚀 Starting... http://0.0.0.0:{PORT}")
    logger.info(f"🧪 Test Mode: {'ON' if TEST_MODE else 'OFF'}")
    logger.info("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
