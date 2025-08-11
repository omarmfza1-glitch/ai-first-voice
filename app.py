# app.py — مركز اتصال ذكي متكامل (النسخة النهائية v11 - نمط Connect)
# -*- coding: utf-8 -*-

# ============================================================================
# 1. الواردات الأساسية
# ============================================================================
import os, base64, uuid, sqlite3, datetime, json, asyncio, logging, re
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
try:
    import audioop
except ModuleNotFoundError:
    import audioop_lts as audioop
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException, Response, Header
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
load_dotenv()

# ============================================================================
# 2. إعداد السجلات والخدمات الخارجية
# ============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("smart-cc")

from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException
from twilio.request_validator import RequestValidator
from openai import OpenAI

# ============================================================================
# 3. متغيرات البيئة والتهيئة العالمية
# ============================================================================
PORT = int(os.getenv("PORT", 8000))
BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None

speech_async_client = None
speech_types = None
try:
    GCP_KEY_JSON_STR = os.getenv("GCP_KEY_JSON")
    if GCP_KEY_JSON_STR:
        with open("gcp.json", "w", encoding="utf-8") as f: f.write(GCP_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
    
    from google.cloud.speech_v1p1beta1.services import speech
    from google.cloud.speech_v1p1beta1 import types as speech_types_module
    speech_types = speech_types_module
    speech_async_client = speech.SpeechAsyncClient()
    logger.info("✅ Google Cloud Speech Async Client initialized successfully.")
except Exception as e:
    logger.warning(f"⚠️ Google Cloud Speech Async Client could not be initialized: {e}")

# ============================================================================
# 4. قاعدة البيانات
# ============================================================================
DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")
def init_database():
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS customers (customer_id TEXT PRIMARY KEY, name TEXT, phone TEXT UNIQUE, current_package TEXT, account_balance REAL, last_bill_date TEXT, status TEXT DEFAULT 'active')")
    cur.execute("CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, call_sid TEXT, turn INTEGER, user_text TEXT, intent TEXT, tool_called TEXT, tool_result TEXT, reply_text TEXT, reply_audio_url TEXT, duration_ms INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS tickets (ticket_id TEXT PRIMARY KEY, customer_id TEXT, summary TEXT, status TEXT DEFAULT 'open', priority TEXT DEFAULT 'normal', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    sample_customers = [("CUST_001", "أحمد محمد", "966501234567", "باقة البلاتينيوم", 350.75, "2025-07-15"), ("CUST_002", "فاطمة علي", "966502345678", "باقة الذهبية", 150.50, "2025-07-10")]
    for cust in sample_customers: cur.execute("INSERT OR IGNORE INTO customers VALUES (?, ?, ?, ?, ?, ?, 'active')", cust)
    conn.commit(); conn.close()
    logger.info("✅ Database initialized successfully.")

# ============================================================================
# 5. تطبيق FastAPI
# ============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("🚀 Starting Smart Call Center..."); init_database(); yield; logger.info("👋 Shutting down Smart Call Center...")
app = FastAPI(title="Smart Call Center API", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
os.makedirs("public/tts", exist_ok=True); app.mount("/public", StaticFiles(directory="public"), name="public")
CALL_STATE: Dict[str, Dict[str, Any]] = {}

# ============================================================================
# 6. الأدوات المساعدة
# ============================================================================
def log_conversation(**kwargs):
    # ... (لم يتغير) ...

# ============================================================================
# 7. نقاط النهاية الرئيسية (تم إعادة الهيكلة بالكامل)
# ============================================================================
@app.post("/twilio/voice")
async def voice_handler(request: Request, x_twilio_signature: Optional[str] = Header(None)):
    """
    نقطة الدخول للمكالمة. الآن تستخدم <Connect> لبدء جلسة WebSocket.
    """
    if twilio_validator:
        form_params = await request.form(); url = str(request.url)
        if "x-forwarded-proto" in request.headers: url = url.replace("http://", f"{request.headers['x-forwarded-proto']}://")
        if not twilio_validator.validate(url, form_params, x_twilio_signature or ""):
            logger.warning(f"❌ Twilio signature validation failed"); raise HTTPException(status_code=403, detail="Invalid Twilio Signature")
    else: form_params = await request.form()
    
    call_sid = form_params.get("CallSid")
    from_number = form_params.get("From")
    if call_sid not in CALL_STATE: CALL_STATE[call_sid] = {"turn": 0, "from_number": from_number, "stream_sid": None}

    ws_url = f"{BASE_URL.replace('http', 'ws')}/twilio/media"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="{ws_url}" />
    </Connect>
    <Say>عذرًا، لا يمكننا الاتصال بالخدمة الآن.</Say>
</Response>""".strip()
    
    logger.info(f"📞 Connect handler: Connecting call {call_sid} to WebSocket.")
    return Response(content=twiml, media_type="text/xml; charset=utf-8")

async def handle_user_turn(ws: WebSocket, stream_sid: str, call_sid: str, user_text: str):
    # ... (تم نقل المنطق إلى هنا ليعمل داخل WebSocket)
    if not call_sid or not user_text: return
    
    call_state = CALL_STATE.get(call_sid)
    if not call_state: logger.warning(f"No state for call {call_sid}."); return

    intent, tool_called, tool_result, reply_text = await _llm_plan_and_reply(user_text, call_state)
    final_reply_text = reply_text or "عذرًا، لم أفهم."
    
    mp3_url = await _synthesize_tts(final_reply_text)
    
    # تحديث: إرسال TwiML عبر WebSocket
    play_twiml = f"<Play>{mp3_url}</Play>" if mp3_url else f'<Say language="ar-SA" voice="Polly.Zeina">{final_reply_text}</Say>'
    
    # إرسال الصوت، ثم علامة لبدء الاستماع مرة أخرى
    await ws.send_json({
        "event": "media",
        "streamSid": stream_sid,
        "media": { "payload": base64.b64encode(audioop.lin2ulaw(b'\x00'*160, 2)).decode('ascii') } # إرسال صمت
    })
    await ws.send_json({ "event": "mark", "streamSid": stream_sid, "mark": { "name": "start_listening" }})

    call_state["turn"] += 1
    # ... (التسجيل في قاعدة البيانات)

@app.websocket("/twilio/media")
async def media_stream_handler(ws: WebSocket):
    await ws.accept()
    call_sid, stream_sid = None, None
    audio_queue = asyncio.Queue()
    
    async def receiver(ws: WebSocket, queue: asyncio.Queue):
        nonlocal call_sid, stream_sid
        try:
            while True:
                message = await ws.receive_json()
                event = message.get("event")
                if event == "connected":
                    logger.info("WS: Connected event received.")
                elif event == "start":
                    stream_sid = message["start"]["streamSid"]
                    call_sid = message["start"]["callSid"]
                    CALL_STATE[call_sid]["stream_sid"] = stream_sid
                    logger.info(f"▶️ WS: Stream started for call {call_sid}, stream {stream_sid}")
                    
                    # إرسال التحية عبر WebSocket
                    welcome_twiml = '<Say language="ar-SA" voice="Polly.Zeina">مرحبًا بكم في مركز الاتصال الذكي.</Say>'
                    await ws.send_json({
                        "event": "media",
                        "streamSid": stream_sid,
                        "media": { "payload": base64.b64encode(audioop.lin2ulaw(b'\x00'*160, 2)).decode('ascii') }
                    })

                elif event == "media":
                    chunk = audioop.ulaw2lin(base64.b64decode(message["media"]["payload"]), 2)
                    await queue.put(chunk)
                
                elif event == "stop":
                    logger.info(f"⏹️ WS: Stream stopped.")
                    await queue.put(None); break
        except WebSocketDisconnect: logger.info("WS receiver disconnected."); await queue.put(None)
        except Exception as e: logger.error(f"WS receiver error: {e}"); await queue.put(None)

    async def stt_sender(queue: asyncio.Queue):
        # ... (نفس الكود)
        
    receiver_task = asyncio.create_task(receiver(ws, audio_queue))

    if speech_async_client and speech_types and not TEST_MODE:
        try:
            responses = await speech_async_client.streaming_recognize(requests=stt_sender(audio_queue))
            async for response in responses:
                # ... (نفس الكود)
                if transcript and call_sid and stream_sid:
                    await handle_user_turn(ws, stream_sid, call_sid, transcript)
        except Exception as e:
            logger.error(f"STT streaming error: {e}")
            
    await receiver_task
    logger.info(f"WebSocket cleanup for call {call_sid} completed.")

# ... باقي الدوال لم تتغير ...
