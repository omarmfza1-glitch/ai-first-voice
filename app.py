# app.py — مركز اتصال ذكي متكامل مع Twilio + Google STT + OpenAI
# -*- coding: utf-8 -*-

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
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

# دعم audioop على Python 3.13+
try:
    import audioop
except ModuleNotFoundError:
    import audioop_lts as audioop

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import Response, PlainTextResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from urllib.parse import urlparse, parse_qs
import re

# تحميل متغيرات البيئة
load_dotenv()

# إعداد السجلات
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("smart-cc")

# ----------------------------------------------------------------------------
# متغيرات البيئة
# ----------------------------------------------------------------------------
# معالجة ملف GCP من متغير بيئة
GCP_KEY_JSON = os.getenv("GCP_KEY_JSON")
if GCP_KEY_JSON:
    try:
        with open("gcp.json", "w", encoding="utf-8") as f:
            f.write(GCP_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
        logger.info("✅ GCP credentials configured successfully")
    except Exception as e:
        logger.error(f"❌ Failed to write GCP credentials: {e}")

PORT = int(os.getenv("PORT", 5000))
BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"

# ----------------------------------------------------------------------------
# قاعدة البيانات
# ----------------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

def init_database():
    """تهيئة قاعدة البيانات مع الجداول المطلوبة"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    # جدول العملاء
    cur.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            customer_id TEXT PRIMARY KEY,
            name TEXT,
            phone TEXT UNIQUE,
            current_package TEXT,
            account_balance REAL,
            last_bill_date TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # جدول المحادثات
    cur.execute("""
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
            duration_ms INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # جدول التذاكر
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tickets (
            ticket_id TEXT PRIMARY KEY,
            customer_id TEXT,
            summary TEXT,
            status TEXT DEFAULT 'open',
            priority TEXT DEFAULT 'normal',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # إضافة بيانات تجريبية للعملاء
    sample_customers = [
        ("966501234567", "أحمد محمد", "966501234567", "باقة البلاتينيوم", 350.75, "2025-01-15"),
        ("966502345678", "فاطمة علي", "966502345678", "باقة الذهبية", 150.50, "2025-01-10"),
        ("966503456789", "خالد سعود", "966503456789", "باقة الفضية", 75.25, "2025-01-20"),
    ]
    
    for customer in sample_customers:
        cur.execute("""
            INSERT OR IGNORE INTO customers (customer_id, name, phone, current_package, account_balance, last_bill_date)
            VALUES (?, ?, ?, ?, ?, ?)
        """, customer)
    
    conn.commit()
    conn.close()
    logger.info("✅ Database initialized successfully")

# تهيئة قاعدة البيانات عند بدء التطبيق
init_database()

# ----------------------------------------------------------------------------
# الخدمات الخارجية
# ----------------------------------------------------------------------------
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException
from openai import OpenAI

# Google Speech-to-Text
GOOGLE_STT_AVAILABLE = False
try:
    from google.cloud import speech_v1p1beta1 as speech
    from google.cloud.speech_v1p1beta1 import StreamingRecognizeRequest
    GOOGLE_STT_AVAILABLE = True
    logger.info("✅ Google Cloud Speech module loaded")
except ImportError:
    logger.warning("⚠️ Google Cloud Speech not available. Install google-cloud-speech")
except Exception as e:
    logger.error(f"❌ Error loading Google Cloud Speech: {e}")

# OpenAI Client
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
if not openai_client:
    logger.warning("⚠️ OpenAI API key not configured")

# Twilio Client
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        logger.info("✅ Twilio client initialized")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Twilio: {e}")

# ----------------------------------------------------------------------------
# FastAPI Application
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle management for the application"""
    logger.info("🚀 Starting Smart Call Center...")
    yield
    logger.info("👋 Shutting down Smart Call Center...")

app = FastAPI(
    title="Smart Call Center API",
    description="مركز اتصال ذكي بالذكاء الاصطناعي",
    version="1.0.0",
    lifespan=lifespan
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files for TTS audio
os.makedirs("public/tts", exist_ok=True)
app.mount("/public", StaticFiles(directory="public"), name="public")

# حالة المكالمات في الذاكرة
CALL_STATE: Dict[str, Dict[str, Any]] = {}

# ----------------------------------------------------------------------------
# أدوات مساعدة
# ----------------------------------------------------------------------------

def log_conversation(
    call_sid: str, 
    turn: int, 
    user_text: str, 
    intent: str, 
    tool_called: str, 
    tool_result: str, 
    reply_text: str, 
    reply_audio_url: str,
    duration_ms: int = 0
):
    """تسجيل تفاصيل المحادثة في قاعدة البيانات"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO conversations 
            (timestamp, call_sid, turn, user_text, intent, tool_called, tool_result, reply_text, reply_audio_url, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.datetime.utcnow().isoformat(),
            call_sid,
            turn,
            user_text,
            intent or "",
            tool_called or "",
            tool_result or "",
            reply_text or "",
            reply_audio_url or "",
            duration_ms
        ))
        conn.commit()
        conn.close()
        logger.info(f"📝 Logged conversation turn {turn} for call {call_sid}")
    except Exception as e:
        logger.error(f"Failed to log conversation: {e}")

class SpeechRequestIterator:
    """مولِّد متزامن للطلبات الصوتية لـ Google STT"""
    def __init__(self):
        self.q: queue.Queue[Optional[bytes]] = queue.Queue()
        self.closed = False

    def push(self, pcm: bytes):
        if not self.closed:
            self.q.put(pcm)

    def close(self):
        if not self.closed:
            self.closed = True
            self.q.put(None)

    def __iter__(self):
        while True:
            chunk = self.q.get()
            if chunk is None:
                break
            yield StreamingRecognizeRequest(audio_content=chunk)

# ----------------------------------------------------------------------------
# نقاط النهاية الرئيسية
# ----------------------------------------------------------------------------

@app.post("/voice")
async def voice_handler(request: Request):
    """معالج Twilio الرئيسي للمكالمات الواردة"""
    form = await request.form()
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")
    
    # تحديث حالة المكالمة
    state = CALL_STATE.get(call_sid)
    if state is None:
        CALL_STATE[call_sid] = {
            "turn": 0,
            "from_number": from_number,
            "start_time": datetime.datetime.utcnow()
        }
        first_turn = True
    else:
        first_turn = state.get("turn", 0) == 0
    
    # إعداد WebSocket URL
    ws_host = urlparse(BASE_URL).netloc or request.url.netloc
    wss_base = f"wss://{ws_host}/media"
    
    # التحية الأولية
    say_block = ""
    if first_turn:
        say_block = """
  <Say language="ar-SA" voice="Polly.Zeina">
    مرحبًا بكم في مركز الاتصال الذكي. 
    تفضّل بالحديث، أنا أُصغي إليك.
  </Say>"""
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Start>
    <Stream url="{wss_base}" track="inbound_track">
      <Parameter name="callSid" value="{call_sid}"/>
    </Stream>
  </Start>{say_block}
  <Pause length="60"/>
</Response>""".strip()
    
    logger.info(f"📞 Voice handler: call_sid={call_sid}, first_turn={first_turn}")
    return Response(content=twiml, media_type="text/xml; charset=utf-8")

@app.websocket("/media")
async def media_stream(ws: WebSocket):
    """WebSocket لاستقبال البث الصوتي من Twilio"""
    global GOOGLE_STT_AVAILABLE  # استخدام المتغير العام
    
    await ws.accept()
    
    # استخراج call_sid من الـ query string
    query = ws.scope.get("query_string", b"").decode()
    call_sid = ""
    if "callSid=" in query:
        try:
            call_sid = query.split("callSid=")[1].split("&")[0]
        except Exception:
            call_sid = ""
    
    logger.info(f"🔌 WebSocket connected: call_sid={call_sid}")
    
    # تهيئة Google STT إذا كان متاحًا
    req_iter = None
    stt_task = None
    speech_client = None
    test_task = None
    stt_available = GOOGLE_STT_AVAILABLE  # نسخة محلية للقراءة
    
    if stt_available and not TEST_MODE:
        try:
            speech_client = speech.SpeechClient()
            streaming_config = speech.StreamingRecognitionConfig(
                config=speech.RecognitionConfig(
                    encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                    sample_rate_hertz=8000,
                    language_code="ar-SA",
                    # تم إزالة alternative_language_codes لأنه غير مدعوم
                    # alternative_language_codes=["en-US"],
                    use_enhanced=True,
                    model="phone_call",
                    enable_automatic_punctuation=True,
                    enable_word_time_offsets=False,
                    profanity_filter=False,
                    speech_contexts=[
                        speech.SpeechContext(
                            phrases=[
                                "السلام عليكم",
                                "رصيدي",
                                "الباقة",
                                "فاتورة",
                                "مشكلة",
                                "إنترنت",
                                "شكرا",
                                "مع السلامة"
                            ]
                        )
                    ]
                ),
                interim_results=True,
                single_utterance=False,
            )
            
            req_iter = SpeechRequestIterator()
            stt_responses = speech_client.streaming_recognize(streaming_config, req_iter)
            stt_task = asyncio.create_task(_consume_stt_responses(stt_responses, lambda: call_sid))
            logger.info("✅ Google STT initialized for call")
        except Exception as e:
            logger.error(f"Failed to initialize STT: {e}")
            stt_available = False
    
    # وضع الاختبار إذا لم يكن STT متاحًا
    if not stt_available or TEST_MODE:
        logger.warning("⚠️ Running in TEST MODE or STT unavailable - using simulated input")
        test_task = asyncio.create_task(_simulate_user_input(call_sid, delay=5))
    
    try:
        while True:
            msg = await ws.receive_text()
            try:
                event = json.loads(msg)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON received: {msg[:100]}")
                continue
                
            etype = event.get("event")
            
            if etype == "start":
                start = event.get("start", {})
                cp = start.get("customParameters")
                
                # معالجة customParameters - قد يكون dict أو string
                if cp:
                    if isinstance(cp, dict):
                        # إذا كان dictionary مباشرة
                        if not call_sid:
                            call_sid = cp.get("callSid", "") or start.get("callSid", "")
                    elif isinstance(cp, str):
                        # إذا كان string، نحلله
                        try:
                            qs = parse_qs(cp)
                            if not call_sid:
                                call_sid = (qs.get("callSid") or [""])[0] or start.get("callSid", "")
                        except Exception as e:
                            logger.warning(f"Failed to parse customParameters: {e}")
                            if not call_sid:
                                call_sid = start.get("callSid", "")
                else:
                    if not call_sid:
                        call_sid = start.get("callSid", "")
                        
                logger.info(f"▶️ Stream started: call_sid={call_sid}")
                
            elif etype == "media":
                # معالجة البيانات الصوتية
                media_payload = event.get("media", {})
                b64 = media_payload.get("payload")
                
                if b64 and req_iter:
                    try:
                        ulaw = base64.b64decode(b64)
                        pcm = audioop.ulaw2lin(ulaw, 2)  # تحويل إلى 16-bit PCM
                        req_iter.push(pcm)
                        # سجل للتأكد من استقبال البيانات
                        if len(pcm) > 0:
                            logger.debug(f"Audio chunk received: {len(pcm)} bytes")
                    except Exception as e:
                        logger.warning(f"Error processing audio: {e}")
                elif b64 and not req_iter:
                    logger.warning("Audio received but STT not initialized")
                        
            elif etype == "stop":
                logger.info(f"⏹️ Stream stopped: call_sid={call_sid}")
                break
                
            elif etype == "mark":
                # Twilio mark events - يمكن تجاهلها
                logger.debug(f"Mark event: {event.get('mark', {})}")
                
            elif etype == "connected":
                logger.info(f"✅ Stream connected: call_sid={call_sid}")
                
    except WebSocketDisconnect:
        logger.info(f"🔌 WebSocket disconnected: call_sid={call_sid}")
    except Exception as e:
        logger.exception(f"WebSocket error [{call_sid}]: {e}")
    finally:
        if req_iter:
            req_iter.close()
        if stt_task:
            try:
                await stt_task
            except Exception as e:
                logger.warning(f"Error closing STT task: {e}")
        if test_task:
            try:
                test_task.cancel()
            except Exception:
                pass
        try:
            await ws.close()
        except Exception:
            pass
        logger.info(f"WebSocket cleanup completed for {call_sid}")

async def _aiter(sync_iterable):
    """تحويل iterator متزامن إلى async"""
    loop = asyncio.get_event_loop()
    iterator = iter(sync_iterable)
    while True:
        try:
            item = await loop.run_in_executor(None, next, iterator)
        except StopIteration:
            break
        yield item

async def _consume_stt_responses(stt_responses, get_call_sid):
    """معالجة نتائج التعرف على الكلام"""
    try:
        async for resp in _aiter(stt_responses):
            for result in resp.results:
                # عرض النتائج المؤقتة للتشخيص
                if not result.is_final:
                    interim_text = result.alternatives[0].transcript.strip()
                    if interim_text:
                        logger.debug(f"🎤 STT Interim: {interim_text}")
                else:
                    # النتيجة النهائية
                    transcript = result.alternatives[0].transcript.strip()
                    if transcript:
                        call_sid = get_call_sid()
                        logger.info(f"🎤 STT Final [{call_sid}]: {transcript}")
                        await _handle_user_turn(call_sid, transcript)
                    else:
                        logger.debug("Empty final transcript received")
    except Exception as e:
        logger.error(f"STT processing error: {e}")
        # في حالة فشل STT، نستخدم وضع الاختبار
        call_sid = get_call_sid()
        if call_sid:
            logger.warning(f"Falling back to test mode for call {call_sid}")
            await _simulate_user_input(call_sid, delay=3)

async def _simulate_user_input(call_sid: str, delay: int = 5):
    """محاكاة إدخال المستخدم للاختبار"""
    if not call_sid:
        logger.warning("No call_sid for simulation")
        return
        
    test_phrases = [
        "السلام عليكم، أريد معرفة رصيدي",
        "عندي مشكلة في الإنترنت",
        "ما هي باقتي الحالية؟",
        "شكراً لك"
    ]
    
    await asyncio.sleep(delay)
    
    for i, phrase in enumerate(test_phrases):
        logger.info(f"🧪 TEST MODE: Simulating user input [{i+1}/{len(test_phrases)}]: {phrase}")
        try:
            await _handle_user_turn(call_sid, phrase)
        except Exception as e:
            logger.error(f"Error in simulated input: {e}")
        await asyncio.sleep(10)  # انتظار 10 ثواني بين كل جملة

# ----------------------------------------------------------------------------
# معالجة الذكاء الاصطناعي والأدوات
# ----------------------------------------------------------------------------

async def _llm_plan_and_reply(user_text: str, call_state: dict) -> tuple:
    """استخدام GPT لفهم النية والرد مع استدعاء الأدوات"""
    intent = None
    tool_called = None
    tool_result = None
    answer = None
    
    if not openai_client:
        return intent, tool_called, tool_result, "عذرًا، الخدمة غير متاحة مؤقتًا."
    
    # تحضير السياق
    from_number = call_state.get("from_number", "")
    turn = call_state.get("turn", 0)
    
    SYSTEM = """أنت مساعد اتصال لشركة اتصالات سعودية رائدة. 
    - استنتج نية العميل بدقة
    - استخدم الأدوات المناسبة للاستعلام عن البيانات
    - قدم إجابات موجزة ومهذبة بالعربية الفصحى
    - كن ودودًا ومحترفًا في نفس الوقت
    - إذا لم تفهم الطلب، اطلب التوضيح بلطف"""
    
    # تعريف الأدوات المتاحة
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_balance",
                "description": "جلب رصيد العميل الحالي ومعلومات الباقة",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string", "description": "رقم هاتف العميل"}
                    },
                    "required": ["phone_number"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "open_ticket",
                "description": "فتح تذكرة دعم فني للعميل",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string", "description": "وصف المشكلة"},
                        "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]}
                    },
                    "required": ["summary"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "check_package_info",
                "description": "الاستعلام عن تفاصيل الباقة الحالية للعميل",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phone_number": {"type": "string"}
                    },
                    "required": ["phone_number"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "transfer_to_agent",
                "description": "تحويل المكالمة إلى موظف خدمة عملاء",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "department": {"type": "string", "enum": ["sales", "technical", "billing", "general"]},
                        "reason": {"type": "string"}
                    },
                    "required": ["department", "reason"]
                }
            }
        }
    ]
    
    try:
        # إضافة رقم المتصل للسياق
        user_message = f"المتصل من الرقم: {from_number}\nالرسالة: {user_text}"
        
        comp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_message}
            ],
            tools=tools,
            tool_choice="auto",
            temperature=0.3,
            max_tokens=200,
        )
        
        choice = comp.choices[0]
        msg = choice.message
        
        # معالجة استدعاء الأدوات
        if choice.finish_reason == "tool_calls" and msg.tool_calls:
            call = msg.tool_calls[0]
            tool_called = call.function.name
            args = json.loads(call.function.arguments or "{}")
            
            if tool_called == "lookup_balance":
                phone = args.get("phone_number", from_number)
                result = await _lookup_customer_balance(phone)
                tool_result = json.dumps(result, ensure_ascii=False)
                intent = "balance_inquiry"
                
                if result.get("success"):
                    balance = result.get("balance", 0)
                    package = result.get("package", "غير محدد")
                    answer = f"رصيدك الحالي هو {balance} ريال سعودي. وأنت مشترك في {package}."
                else:
                    answer = "عذرًا، لم أتمكن من العثور على معلومات حسابك. هل يمكنك التأكد من رقم الهاتف؟"
                    
            elif tool_called == "open_ticket":
                ticket_id = await _create_support_ticket(
                    from_number, 
                    args.get("summary", ""),
                    args.get("priority", "normal")
                )
                tool_result = ticket_id
                intent = "open_ticket"
                answer = f"تم فتح تذكرة الدعم رقم {ticket_id}. سيتواصل معك فريق الدعم الفني خلال 24 ساعة."
                
            elif tool_called == "check_package_info":
                phone = args.get("phone_number", from_number)
                info = await _get_package_info(phone)
                tool_result = json.dumps(info, ensure_ascii=False)
                intent = "package_inquiry"
                
                if info.get("success"):
                    answer = f"أنت مشترك في {info.get('package_name')}. {info.get('description', '')}"
                else:
                    answer = "عذرًا، لم أتمكن من الحصول على معلومات الباقة."
                    
            elif tool_called == "transfer_to_agent":
                dept = args.get("department", "general")
                reason = args.get("reason", "")
                intent = "transfer_request"
                answer = f"سأقوم بتحويلك إلى قسم {_get_dept_name(dept)}. من فضلك انتظر قليلاً."
                
        # إذا لم يتم استدعاء أي أداة
        if not answer:
            intent = intent or "general_inquiry"
            answer = (msg.content or "").strip()
            if not answer:
                answer = "كيف يمكنني مساعدتك اليوم؟"
                
    except Exception as e:
        logger.error(f"LLM error: {e}")
        answer = "عذرًا، حدث خطأ. هل يمكنك إعادة المحاولة؟"
    
    return intent, tool_called, tool_result, answer

def _get_dept_name(dept: str) -> str:
    """ترجمة أسماء الأقسام"""
    departments = {
        "sales": "المبيعات",
        "technical": "الدعم الفني",
        "billing": "الفواتير والحسابات",
        "general": "خدمة العملاء"
    }
    return departments.get(dept, "خدمة العملاء")

async def _lookup_customer_balance(phone: str) -> dict:
    """البحث عن رصيد العميل من قاعدة البيانات"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        # تنظيف رقم الهاتف
        clean_phone = re.sub(r'\D', '', phone)
        if not clean_phone.startswith("966"):
            clean_phone = "966" + clean_phone
            
        cur.execute("""
            SELECT customer_id, name, account_balance, current_package 
            FROM customers 
            WHERE phone = ? OR phone = ?
        """, (phone, clean_phone))
        
        row = cur.fetchone()
        conn.close()
        
        if row:
            return {
                "success": True,
                "customer_id": row[0],
                "name": row[1],
                "balance": row[2],
                "package": row[3]
            }
        else:
            return {"success": False, "message": "Customer not found"}
            
    except Exception as e:
        logger.error(f"Database error: {e}")
        return {"success": False, "message": "Database error"}

async def _get_package_info(phone: str) -> dict:
    """الحصول على معلومات الباقة"""
    customer = await _lookup_customer_balance(phone)
    if customer.get("success"):
        package_name = customer.get("package", "")
        
        # معلومات الباقات (يمكن نقلها لقاعدة البيانات)
        packages = {
            "باقة البلاتينيوم": {
                "description": "باقة غير محدودة للإنترنت والمكالمات المحلية، مع 1000 دقيقة دولية",
                "price": 399,
                "data": "غير محدود",
                "minutes": "غير محدود محلي + 1000 دولي"
            },
            "باقة الذهبية": {
                "description": "200 جيجا إنترنت، 500 دقيقة محلية، 100 دقيقة دولية",
                "price": 199,
                "data": "200 GB",
                "minutes": "500 محلي + 100 دولي"
            },
            "باقة الفضية": {
                "description": "50 جيجا إنترنت، 200 دقيقة محلية",
                "price": 99,
                "data": "50 GB",
                "minutes": "200 محلي"
            }
        }
        
        info = packages.get(package_name, {})
        return {
            "success": True,
            "package_name": package_name,
            "description": info.get("description", ""),
            "price": info.get("price", 0),
            "data": info.get("data", ""),
            "minutes": info.get("minutes", "")
        }
    
    return {"success": False}

async def _create_support_ticket(phone: str, summary: str, priority: str = "normal") -> str:
    """إنشاء تذكرة دعم جديدة"""
    try:
        ticket_id = f"T-{str(uuid.uuid4())[:8].upper()}"
        
        # البحث عن معرف العميل
        customer = await _lookup_customer_balance(phone)
        customer_id = customer.get("customer_id", phone) if customer.get("success") else phone
        
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tickets (ticket_id, customer_id, summary, priority, status)
            VALUES (?, ?, ?, ?, 'open')
        """, (ticket_id, customer_id, summary, priority))
        conn.commit()
        conn.close()
        
        logger.info(f"✅ Created ticket {ticket_id} for customer {customer_id}")
        return ticket_id
        
    except Exception as e:
        logger.error(f"Failed to create ticket: {e}")
        return f"T-{str(uuid.uuid4())[:8].upper()}"

async def _arabic_diacritize_and_style(text: str) -> str:
    """إضافة التشكيل العربي وعلامات الإلقاء"""
    if not openai_client:
        return text
        
    prompt = f"""أضف التشكيل العربي للنص التالي بدقة، مع الحفاظ على الوضوح.
أضف علامات توقف [pause=300ms] في الأماكن المناسبة فقط (بين الجمل الطويلة).
لا تغير المعنى أو تضيف كلمات جديدة.

النص: {text}

النص المُشكَّل:"""
    
    try:
        comp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=300,
        )
        styled = (comp.choices[0].message.content or text).strip()
        
        # إزالة علامات التوقف من النص للـ TTS (OpenAI TTS لا يدعمها)
        styled_clean = re.sub(r'\[pause=\d+ms\]', '', styled)
        return styled_clean
        
    except Exception as e:
        logger.error(f"Diacritization error: {e}")
        return text

async def _synthesize_tts(text: str) -> Optional[str]:
    """توليد الصوت باستخدام OpenAI TTS"""
    if not openai_client:
        return None
        
    try:
        file_id = f"{uuid.uuid4()}.mp3"
        out_path = os.path.join("public", "tts", file_id)
        url = f"{BASE_URL}/public/tts/{file_id}"
        
        # استخدام صوت عربي مناسب
        response = openai_client.audio.speech.create(
            model="tts-1",  # أو "tts-1-hd" لجودة أعلى
            voice="alloy",  # يمكن تجربة: nova, shimmer, echo, fable, onyx
            input=text,
            response_format="mp3"
        )
        
        # حفظ الملف الصوتي
        response.stream_to_file(out_path)
        logger.info(f"✅ TTS generated: {url}")
        return url
        
    except Exception as e:
        logger.error(f"TTS error: {e}")
        return None

# ----------------------------------------------------------------------------
# معالجة دورة المحادثة
# ----------------------------------------------------------------------------

async def _handle_user_turn(call_sid: str, user_text: str):
    """معالجة دورة واحدة من المحادثة"""
    if not call_sid:
        logger.warning("No call_sid while handling turn")
        return
    
    start_time = datetime.datetime.utcnow()
    
    # الحصول على حالة المكالمة
    call_state = CALL_STATE.get(call_sid, {})
    
    # معالجة بـ LLM
    intent, tool_called, tool_result, reply_text = await _llm_plan_and_reply(user_text, call_state)
    
    # تحسين النص العربي
    prepared_text = await _arabic_diacritize_and_style(reply_text)
    
    # توليد الصوت
    mp3_url = await _synthesize_tts(prepared_text)
    
    # تحديث المكالمة عبر Twilio
    if twilio_client and call_sid:
        try:
            if mp3_url:
                # استخدام ملف الصوت المولّد
                twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Play>{mp3_url}</Play>
  <Redirect method="POST">{BASE_URL}/voice</Redirect>
</Response>"""
            else:
                # استخدام TTS المدمج في Twilio كخيار احتياطي
                safe_text = prepared_text or reply_text or "هل يمكنك التوضيح أكثر؟"
                twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say language="ar-SA" voice="Polly.Zeina">{safe_text}</Say>
  <Redirect method="POST">{BASE_URL}/voice</Redirect>
</Response>"""
            
            # تحديث المكالمة
            twilio_client.calls(call_sid).update(twiml=twiml)
            logger.info(f"✅ Call updated [{call_sid}]: TTS={bool(mp3_url)}")
            
        except TwilioException as e:
            logger.error(f"Twilio update error [{call_sid}]: {e}")
    
    # تحديث حالة المكالمة
    call_state["turn"] = call_state.get("turn", 0) + 1
    CALL_STATE[call_sid] = call_state
    
    # حساب المدة
    duration_ms = int((datetime.datetime.utcnow() - start_time).total_seconds() * 1000)
    
    # تسجيل المحادثة
    log_conversation(
        call_sid,
        call_state["turn"],
        user_text,
        intent,
        tool_called,
        tool_result,
        reply_text,
        mp3_url or "",
        duration_ms
    )

# ----------------------------------------------------------------------------
# نقاط نهاية إضافية
# ----------------------------------------------------------------------------

@app.post("/twilio/status")
async def twilio_status_callback(request: Request):
    """معالج حالة المكالمة من Twilio"""
    try:
        form = await request.form()
        call_sid = form.get("CallSid", "")
        call_status = form.get("CallStatus", "")
        
        logger.info(f"📞 Call status: {call_sid} -> {call_status}")
        
        # تنظيف الحالة عند انتهاء المكالمة
        if call_status in ["completed", "failed", "busy", "no-answer"]:
            if call_sid in CALL_STATE:
                del CALL_STATE[call_sid]
                logger.info(f"🧹 Cleaned up state for call {call_sid}")
                
    except Exception as e:
        logger.error(f"Status callback error: {e}")
        
    return PlainTextResponse("")

@app.get("/health")
async def health_check():
    """فحص صحة التطبيق"""
    checks = {
        "status": "healthy",
        "timestamp": datetime.datetime.utcnow().isoformat(),
        "services": {
            "database": False,
            "openai": bool(openai_client),
            "twilio": bool(twilio_client),
            "google_stt": GOOGLE_STT_AVAILABLE
        },
        "test_mode": TEST_MODE
    }
    
    # فحص قاعدة البيانات
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM conversations")
        count = cur.fetchone()[0]
        conn.close()
        checks["services"]["database"] = True
        checks["conversations_count"] = count
    except Exception:
        pass
    
    # تحديد الحالة العامة
    all_healthy = all(checks["services"].values())
    status_code = 200 if all_healthy else 503
    
    return JSONResponse(content=checks, status_code=status_code)

@app.get("/")
async def root():
    """الصفحة الرئيسية"""
    return JSONResponse({
        "name": "Smart Call Center API",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "voice": "/voice",
            "media": "/media",
            "health": "/health",
            "stats": "/api/stats",
            "conversations": "/api/conversations"
        }
    })

# ----------------------------------------------------------------------------
# واجهات API للإدارة
# ----------------------------------------------------------------------------

@app.get("/api/stats")
async def get_statistics():
    """إحصائيات عامة عن النظام"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        # إحصائيات المحادثات
        cur.execute("SELECT COUNT(*) FROM conversations")
        total_conversations = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(DISTINCT call_sid) FROM conversations")
        total_calls = cur.fetchone()[0]
        
        # إحصائيات التذاكر
        cur.execute("SELECT COUNT(*) FROM tickets")
        total_tickets = cur.fetchone()[0]
        
        cur.execute("SELECT COUNT(*) FROM tickets WHERE status='open'")
        open_tickets = cur.fetchone()[0]
        
        # إحصائيات العملاء
        cur.execute("SELECT COUNT(*) FROM customers")
        total_customers = cur.fetchone()[0]
        
        # الأدوات الأكثر استخدامًا
        cur.execute("""
            SELECT tool_called, COUNT(*) as count 
            FROM conversations 
            WHERE tool_called != '' 
            GROUP BY tool_called 
            ORDER BY count DESC 
            LIMIT 5
        """)
        top_tools = [{"tool": row[0], "count": row[1]} for row in cur.fetchall()]
        
        # النوايا الأكثر شيوعًا
        cur.execute("""
            SELECT intent, COUNT(*) as count 
            FROM conversations 
            WHERE intent != '' 
            GROUP BY intent 
            ORDER BY count DESC 
            LIMIT 5
        """)
        top_intents = [{"intent": row[0], "count": row[1]} for row in cur.fetchall()]
        
        conn.close()
        
        return JSONResponse({
            "conversations": {
                "total": total_conversations,
                "unique_calls": total_calls,
                "active_calls": len(CALL_STATE)
            },
            "tickets": {
                "total": total_tickets,
                "open": open_tickets,
                "closed": total_tickets - open_tickets
            },
            "customers": {
                "total": total_customers
            },
            "top_tools": top_tools,
            "top_intents": top_intents,
            "timestamp": datetime.datetime.utcnow().isoformat()
        })
        
    except Exception as e:
        logger.error(f"Stats error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/conversations")
async def get_conversations(limit: int = 50, offset: int = 0):
    """الحصول على سجل المحادثات"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        cur.execute("""
            SELECT id, timestamp, call_sid, turn, user_text, intent, 
                   tool_called, tool_result, reply_text, duration_ms
            FROM conversations 
            ORDER BY id DESC 
            LIMIT ? OFFSET ?
        """, (limit, offset))
        
        conversations = []
        for row in cur.fetchall():
            conversations.append({
                "id": row[0],
                "timestamp": row[1],
                "call_sid": row[2],
                "turn": row[3],
                "user_text": row[4],
                "intent": row[5],
                "tool_called": row[6],
                "tool_result": row[7],
                "reply_text": row[8],
                "duration_ms": row[9]
            })
        
        conn.close()
        
        return JSONResponse({
            "conversations": conversations,
            "limit": limit,
            "offset": offset,
            "total": len(conversations)
        })
        
    except Exception as e:
        logger.error(f"Conversations API error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/conversations/{call_sid}")
async def get_call_conversation(call_sid: str):
    """الحصول على محادثة كاملة لمكالمة معينة"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        
        cur.execute("""
            SELECT id, timestamp, turn, user_text, intent, 
                   tool_called, tool_result, reply_text, duration_ms
            FROM conversations 
            WHERE call_sid = ?
            ORDER BY turn
        """, (call_sid,))
        
        turns = []
        for row in cur.fetchall():
            turns.append({
                "id": row[0],
                "timestamp": row[1],
                "turn": row[2],
                "user_text": row[3],
                "intent": row[4],
                "tool_called": row[5],
                "tool_result": row[6],
                "reply_text": row[7],
                "duration_ms": row[8]
            })
        
        conn.close()
        
        if not turns:
            raise HTTPException(status_code=404, detail="Call not found")
        
        return JSONResponse({
            "call_sid": call_sid,
            "turns": turns,
            "total_turns": len(turns)
        })
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Call conversation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ----------------------------------------------------------------------------
# نقاط نهاية الاختبار
# ----------------------------------------------------------------------------

@app.post("/test/simulate-call")
async def simulate_call(phone: str = "+966501234567"):
    """محاكاة مكالمة للاختبار"""
    if not TEST_MODE:
        return JSONResponse(
            {"error": "Test mode is disabled. Set TEST_MODE=true in environment variables"},
            status_code=403
        )
    
    # إنشاء call_sid وهمي
    test_call_sid = f"TEST_{uuid.uuid4().hex[:8]}"
    
    # تهيئة حالة المكالمة
    CALL_STATE[test_call_sid] = {
        "turn": 0,
        "from_number": phone,
        "start_time": datetime.datetime.utcnow()
    }
    
    # بدء محاكاة في الخلفية
    asyncio.create_task(_run_test_scenario(test_call_sid))
    
    return JSONResponse({
        "status": "Test started",
        "call_sid": test_call_sid,
        "message": "Check logs for results",
        "phone": phone
    })

async def _run_test_scenario(call_sid: str):
    """تشغيل سيناريو اختبار كامل"""
    test_scenarios = [
        ("السلام عليكم", 2),
        ("أريد معرفة رصيدي", 3),
        ("عندي مشكلة في الإنترنت بطيء", 3),
        ("ما هي باقتي الحالية؟", 3),
        ("شكراً لك", 2)
    ]
    
    for user_input, delay in test_scenarios:
        logger.info(f"🧪 TEST [{call_sid}]: User says: {user_input}")
        await _handle_user_turn(call_sid, user_input)
        await asyncio.sleep(delay)
    
    logger.info(f"🧪 TEST [{call_sid}]: Scenario completed")
    
    # تنظيف
    if call_sid in CALL_STATE:
        del CALL_STATE[call_sid]

# ----------------------------------------------------------------------------
# تشغيل التطبيق
# ----------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    import sys
    
    # وضع الاختبار المباشر من سطر الأوامر
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        TEST_MODE = True
        logger.info("=" * 60)
        logger.info("🧪 RUNNING IN TEST MODE")
        logger.info("=" * 60)
    
    logger.info("=" * 50)
    logger.info("🚀 Starting Smart Call Center")
    logger.info(f"📍 Port: {PORT}")
    logger.info(f"🌐 Base URL: {BASE_URL}")
    logger.info(f"✅ OpenAI: {'Connected' if openai_client else '❌ Not configured'}")
    logger.info(f"✅ Twilio: {'Connected' if twilio_client else '❌ Not configured'}")
    logger.info(f"✅ Google STT: {'Available' if GOOGLE_STT_AVAILABLE else '❌ Not available'}")
    logger.info(f"🧪 Test Mode: {'ON' if TEST_MODE else 'OFF'}")
    logger.info("=" * 50)
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
        access_log=True
    )
