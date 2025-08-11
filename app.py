# app.py — مركز اتصال ذكي متكامل (النسخة النهائية)
# -*- coding: utf-8 -*-

# ============================================================================
# 1. الواردات الأساسية (Core Imports)
# ============================================================================
import os
import base64
import uuid
import sqlite3
import datetime
import json
import asyncio
import logging
import queue
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
from urllib.parse import urlparse
import re

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

GOOGLE_STT_AVAILABLE = False
try:
    from google.cloud import speech_v1p1beta1 as speech
    GOOGLE_STT_AVAILABLE = True
    logger.info("✅ Google Cloud Speech module loaded successfully.")
except (ImportError, Exception) as e:
    logger.warning(f"⚠️ Google Cloud Speech module could not be loaded: {e}")

# ============================================================================
# 3. متغيرات البيئة والتهيئة
# ============================================================================
PORT = int(os.getenv("PORT", 8000))
BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"

GCP_KEY_JSON_STR = os.getenv("GCP_KEY_JSON")
if GCP_KEY_JSON_STR and GOOGLE_STT_AVAILABLE:
    try:
        with open("gcp.json", "w", encoding="utf-8") as f: f.write(GCP_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
        logger.info("✅ GCP credentials configured successfully from environment variable.")
    except Exception as e:
        logger.error(f"❌ Failed to write GCP credentials: {e}")
elif GOOGLE_STT_AVAILABLE:
     logger.warning("⚠️ GCP credentials not found in environment. Google STT might not work.")

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None

if not openai_client: logger.warning("⚠️ OpenAI API key not configured.")
if not twilio_client: logger.warning("⚠️ Twilio client not initialized.")

# ============================================================================
# 4. قاعدة البيانات
# ============================================================================
DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")

def init_database():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS customers (customer_id TEXT PRIMARY KEY, name TEXT, phone TEXT UNIQUE, current_package TEXT, account_balance REAL, last_bill_date TEXT, status TEXT DEFAULT 'active')")
    cur.execute("CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, call_sid TEXT, turn INTEGER, user_text TEXT, intent TEXT, tool_called TEXT, tool_result TEXT, reply_text TEXT, reply_audio_url TEXT, duration_ms INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS tickets (ticket_id TEXT PRIMARY KEY, customer_id TEXT, summary TEXT, status TEXT DEFAULT 'open', priority TEXT DEFAULT 'normal', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    sample_customers = [("CUST_001", "أحمد محمد", "966501234567", "باقة البلاتينيوم", 350.75, "2025-07-15"), ("CUST_002", "فاطمة علي", "966502345678", "باقة الذهبية", 150.50, "2025-07-10")]
    for cust in sample_customers: cur.execute("INSERT OR IGNORE INTO customers VALUES (?, ?, ?, ?, ?, ?, 'active')", cust)
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized successfully.")

# ============================================================================
# 5. تطبيق FastAPI
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

CALL_STATE: Dict[str, Dict[str, Any]] = {}

# ============================================================================
# 6. الأدوات المساعدة
# ============================================================================
def log_conversation(**kwargs):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("INSERT INTO conversations (timestamp, call_sid, turn, user_text, intent, tool_called, tool_result, reply_text, reply_audio_url, duration_ms) VALUES (:timestamp, :call_sid, :turn, :user_text, :intent, :tool_called, :tool_result, :reply_text, :reply_audio_url, :duration_ms)", kwargs)
        conn.commit()
        conn.close()
        logger.info(f"📝 Logged turn {kwargs.get('turn')} for call {kwargs.get('call_sid')}")
    except Exception as e:
        logger.error(f"Failed to log conversation: {e}")

class SpeechRequestIterator:
    def __init__(self): self.q: queue.Queue[Optional[bytes]] = queue.Queue(); self.closed = False
    def push(self, pcm: bytes):
        if not self.closed: self.q.put(pcm)
    def close(self):
        if not self.closed: self.closed = True; self.q.put(None)
    def __iter__(self):
        while True:
            chunk = self.q.get()
            if chunk is None: break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

# ============================================================================
# 7. نقاط النهاية الرئيسية (Core Endpoints) - تم تصحيحها
# ============================================================================
@app.post("/twilio/voice")
async def voice_handler(request: Request, x_twilio_signature: Optional[str] = Header(None)):
    if twilio_validator:
        form_params = await request.form()
        url = str(request.url)
        if "x-forwarded-proto" in request.headers:
            url = url.replace("http://", f"{request.headers['x-forwarded-proto']}://")
        if not twilio_validator.validate(url, form_params, x_twilio_signature or ""):
            logger.warning(f"❌ Twilio signature validation failed for /twilio/voice")
            raise HTTPException(status_code=403, detail="Invalid Twilio Signature")
    else:
        form_params = await request.form()

    call_sid = form_params.get("CallSid", f"local_{uuid.uuid4()}")
    from_number = form_params.get("From", "unknown")
    
    state = CALL_STATE.get(call_sid)
    if state is None:
        CALL_STATE[call_sid] = {"turn": 0, "from_number": from_number}
        first_turn = True
    else:
        first_turn = False
    
    # تحديث: إرسال callSid عبر <Parameter> بدلاً من query string
    ws_url = f"{BASE_URL.replace('http', 'ws')}/twilio/media"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start>
        <Stream url="{ws_url}">
            <Parameter name="callSid" value="{call_sid}"/>
        </Stream>
    </Start>
    {'<Say language="ar-SA" voice="Polly.Zeina">مرحبًا بكم في مركز الاتصال الذكي. كيف يمكنني مساعدتك اليوم؟</Say>' if first_turn else ''}
    <Pause length="60"/>
</Response>""".strip()
    
    logger.info(f"📞 Voice handler: Sending TwiML for call_sid={call_sid}")
    return Response(content=twiml, media_type="text/xml; charset=utf-8")


@app.websocket("/twilio/media")
async def media_stream_handler(ws: WebSocket):
    await ws.accept()
    call_sid = None  # سيتم استخلاصه من رسالة 'start'
    req_iter, stt_task = None, None

    try:
        while True:
            message = await ws.receive_json()
            event = message.get("event")

            if event == "start":
                # تحديث: استخلاص callSid من رسالة البدء
                start_payload = message.get("start", {})
                call_sid = start_payload.get("customParameters", {}).get("callSid")
                if not call_sid:
                    logger.error("'start' event received without callSid in customParameters. Closing.")
                    break
                
                logger.info(f"▶️ Twilio stream started for call: {call_sid}")
                
                # الآن فقط نبدأ STT بعد الحصول على callSid
                stt_available = GOOGLE_STT_AVAILABLE and not TEST_MODE
                if stt_available:
                    try:
                        speech_client = speech.SpeechClient()
                        streaming_config = speech.StreamingRecognitionConfig(
                            config=speech.RecognitionConfig(encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16, sample_rate_hertz=8000, language_code="ar-SA", model="telephony", use_enhanced=True, enable_automatic_punctuation=True),
                            interim_results=False
                        )
                        req_iter = SpeechRequestIterator()
                        stt_responses = speech_client.streaming_recognize(streaming_config, req_iter)
                        stt_task = asyncio.create_task(_consume_stt_responses(stt_responses, call_sid))
                        logger.info(f"✅ Google STT stream initialized for {call_sid}.")
                    except Exception as e:
                        logger.error(f"Failed to initialize STT for {call_sid}: {e}")
                        stt_available = False
                
                if not stt_available:
                    logger.warning(f"⚠️ STT is unavailable. Falling back to TEST MODE for call {call_sid}")
                    asyncio.create_task(_simulate_user_input(call_sid, delay=5))

            elif event == "media":
                if req_iter:
                    req_iter.push(audioop.ulaw2lin(base64.b64decode(message["media"]["payload"]), 2))
            
            elif event == "stop":
                logger.info(f"⏹️ Twilio stream stopped for {call_sid}.")
                break
    except WebSocketDisconnect:
        logger.info(f"🔌 WebSocket disconnected for call {call_sid}.")
    except Exception as e:
        logger.exception(f"WebSocket error for {call_sid}: {e}")
    finally:
        if req_iter: req_iter.close()
        if stt_task: await asyncio.gather(stt_task, return_exceptions=True)
        if ws.client_state != "DISCONNECTED": await ws.close()
        logger.info(f"WebSocket cleanup completed for {call_sid}")


@app.post("/twilio/status")
async def status_callback_handler(request: Request, x_twilio_signature: Optional[str] = Header(None)):
    if twilio_validator:
        form_params = await request.form()
        url = str(request.url)
        if "x-forwarded-proto" in request.headers:
            url = url.replace("http://", f"{request.headers['x-forwarded-proto']}://")
        if not twilio_validator.validate(url, form_params, x_twilio_signature or ""):
            logger.warning(f"❌ Twilio signature validation failed for /twilio/status")
            raise HTTPException(status_code=403, detail="Invalid Twilio Signature")
    else:
        form_params = await request.form()
    
    call_sid = form_params.get("CallSid")
    call_status = form_params.get("CallStatus")
    logger.info(f"📞 Call Status: {call_sid} -> {call_status}")
    
    if call_status in ["completed", "failed", "no-answer", "canceled", "busy"]:
        if call_sid in CALL_STATE:
            del CALL_STATE[call_sid]
            logger.info(f"🧹 Cleaned up state for completed call {call_sid}")
            
    return PlainTextResponse("")

# ============================================================================
# 8. منطق المحادثة والذكاء الاصطناعي
# ============================================================================
async def _consume_stt_responses(stt_responses, call_sid: str):
    try:
        for resp in stt_responses:
            if resp.results and resp.results[0].alternatives:
                transcript = resp.results[0].alternatives[0].transcript.strip()
                if transcript:
                    logger.info(f"🎤 STT Final [{call_sid}]: '{transcript}'")
                    await _handle_user_turn(call_sid, transcript)
    except Exception as e:
        logger.error(f"STT consumption error for {call_sid}: {e}")

async def _handle_user_turn(call_sid: str, user_text: str):
    if not call_sid or not user_text: return
    start_time = datetime.datetime.utcnow()
    call_state = CALL_STATE.get(call_sid)
    if not call_state:
        logger.warning(f"No state found for call {call_sid}. Aborting turn.")
        return
    intent, tool_called, tool_result, reply_text = await _llm_plan_and_reply(user_text, call_state)
    final_reply_text = reply_text or "عذرًا، لم أفهم. هل يمكنك إعادة الصياغة؟"
    mp3_url = await _synthesize_tts(final_reply_text)
    if twilio_client and call_sid and not call_sid.startswith("local_"):
        try:
            twiml_response = f"""<?xml version="1.0" encoding="UTF-8"?><Response>{f'<Play>{mp3_url}</Play>' if mp3_url else f'<Say language="ar-SA" voice="Polly.Zeina">{final_reply_text}</Say>'}<Redirect method="POST">{BASE_URL}/twilio/voice</Redirect></Response>"""
            twilio_client.calls(call_sid).update(twiml=twiml_response)
            logger.info(f"✅ Call updated for {call_sid} with new TwiML.")
        except TwilioException as e:
            logger.error(f"Twilio update error for {call_sid}: {e}")
    call_state["turn"] += 1
    duration_ms = int((datetime.datetime.utcnow() - start_time).total_seconds() * 1000)
    log_conversation(timestamp=start_time.isoformat(), call_sid=call_sid, turn=call_state["turn"], user_text=user_text, intent=intent, tool_called=tool_called, tool_result=tool_result, reply_text=final_reply_text, reply_audio_url=mp3_url or "", duration_ms=duration_ms)

async def _llm_plan_and_reply(user_text: str, call_state: dict) -> tuple:
    if not openai_client: return "error", None, None, "خدمة الذكاء الاصطناعي غير متاحة حاليًا."
    from_number = call_state.get("from_number", "")
    SYSTEM_PROMPT = f"أنت مساعد صوتي ذكي في مركز اتصال لشركة اتصالات سعودية. مهمتك هي فهم نية العميل بدقة واستخدام الأدوات المتاحة للإجابة على استفساراته. كن مهذبًا، موجزًا، وتحدث بالعربية الفصحى المبسطة. رقم هاتف المتصل هو: {from_number}"
    tools = [{"type": "function", "function": {"name": "lookup_balance", "description": "الاستعلام عن الرصيد الحالي والباقة للمتصل."}}, {"type": "function", "function": {"name": "open_ticket", "description": "فتح تذكرة دعم فني لمشكلة يصفها العميل.", "parameters": {"type": "object", "properties": {"summary": {"type": "string", "description": "وصف موجز للمشكلة من العميل."}}, "required": ["summary"]}}}, {"type": "function", "function": {"name": "end_call", "description": "إنهاء المكالمة عندما يطلب العميل ذلك أو يشكرك."}}]
    try:
        response = openai_client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_text}], tools=tools, tool_choice="auto", temperature=0.1)
        msg = response.choices[0].message
        if msg.tool_calls:
            tool_call = msg.tool_calls[0]
            tool_name = tool_call.function.name
            if tool_name == "lookup_balance": result = await _tool_lookup_balance(from_number); return "balance_inquiry", tool_name, json.dumps(result), result["message"]
            if tool_name == "open_ticket": args = json.loads(tool_call.function.arguments); result = await _tool_open_ticket(from_number, args.get("summary")); return "open_ticket", tool_name, json.dumps(result), result["message"]
            if tool_name == "end_call": return "end_call", tool_name, "{}", "شكرًا لاتصالك. مع السلامة."
        return "general_inquiry", None, None, msg.content or "كيف يمكنني مساعدتك؟"
    except Exception as e:
        logger.error(f"LLM Error: {e}")
        return "error", None, None, "عذرًا، أواجه صعوبة فنية. يرجى المحاولة مرة أخرى."

async def _synthesize_tts(text: str) -> Optional[str]:
    if not openai_client or not text: return None
    try:
        prompt = f"أضف التشكيل الأساسي والترقيم للنص العربي التالي لجعله مناسبًا للقراءة الصوتية، دون تغيير أي كلمات: '{text}'"
        comp = openai_client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.0, max_tokens=len(text) * 2)
        styled_text = (comp.choices[0].message.content or text).strip().replace('"', '')
        file_id = f"{uuid.uuid4()}.mp3"
        out_path = os.path.join("public", "tts", file_id)
        response = openai_client.audio.speech.create(model="tts-1", voice="alloy", input=styled_text)
        response.stream_to_file(out_path)
        url = f"{BASE_URL}/public/tts/{file_id}"
        logger.info(f"✅ TTS generated: {url}")
        return url
    except Exception as e:
        logger.error(f"TTS synthesis error: {e}"); return None

# ============================================================================
# 9. أدوات الذكاء الاصطناعي (AI Tool Implementations)
# ============================================================================
async def _tool_lookup_balance(phone: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name, account_balance, current_package FROM customers WHERE phone LIKE ?", (f'%{phone[-9:]}',))
    row = cur.fetchone(); conn.close()
    if row: name, balance, package = row; return {"success": True, "message": f"أهلاً بك يا {name.split()[0]}. رصيدك الحالي هو {balance:.2f} ريال، وأنت على {package}."}
    return {"success": False, "message": "عذرًا، لم أتمكن من العثور على حساب مرتبط بهذا الرقم."}

async def _tool_open_ticket(phone: str, summary: str) -> dict:
    if not summary: return {"success": False, "message": "يرجى وصف المشكلة لفتح تذكرة."}
    try:
        ticket_id = f"T-{str(uuid.uuid4())[:6].upper()}"
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("INSERT INTO tickets (ticket_id, customer_id, summary) VALUES (?, ?, ?)", (ticket_id, phone, summary))
        conn.commit(); conn.close()
        logger.info(f"✅ Created ticket {ticket_id} for {phone}")
        return {"success": True, "message": f"تم فتح تذكرة دعم فني لك برقم {ticket_id}. سنتواصل معك قريبًا."}
    except Exception as e:
        logger.error(f"Failed to create ticket: {e}"); return {"success": False, "message": "حدث خطأ أثناء محاولة فتح التذكرة."}

# ============================================================================
# 10. وضع الاختبار (Test Mode)
# ============================================================================
async def _simulate_user_input(call_sid: str, delay: int = 5):
    logger.info(f"🧪 Starting test simulation for call {call_sid}")
    await asyncio.sleep(delay)
    test_phrases = ["السلام عليكم، كم رصيدي؟", "عندي مشكلة في الإنترنت، بطيء جدًا", "شكرًا جزيلاً"]
    for phrase in test_phrases:
        if call_sid not in CALL_STATE: break
        logger.info(f"🧪 TEST MODE: Simulating user input: '{phrase}'")
        await _handle_user_turn(call_sid, phrase)
        await asyncio.sleep(15)

# ============================================================================
# 11. نقاط نهاية إضافية للإدارة
# ============================================================================
@app.get("/")
async def root(): return {"message": "Smart Call Center API is running."}

@app.get("/health")
async def health_check():
    return {"status": "healthy", "services": {"database": "ok" if os.path.exists(DB_PATH) else "error", "openai": "ok" if openai_client else "disabled", "twilio": "ok" if twilio_client else "disabled", "google_stt": "ok" if GOOGLE_STT_AVAILABLE else "disabled"}}

# ============================================================================
# 12. تشغيل التطبيق
# ============================================================================
if __name__ == "__main__":
    import uvicorn
    logger.info("=" * 50)
    logger.info(f"🚀 Starting Smart Call Center in development mode on http://0.0.0.0:{PORT}")
    logger.info(f"🧪 Test Mode: {'ON' if TEST_MODE else 'OFF'}")
    logger.info("=" * 50)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
