```python
# app.py
import os, io, base64, uuid, sqlite3, datetime, json
# دعم audioop على بايثون 3.13 عبر مكتبة بديلة
try:
    import audioop  # Python <= 3.12
except ModuleNotFoundError:
    import audioop_lts as audioop  # بديل متوافق
from typing import Optional, List
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, PlainTextResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv


load_dotenv()


app = FastAPI()
os.makedirs("public/tts", exist_ok=True)  # ينشئ public أيضًا إن لم يوجد
app.mount("/public", StaticFiles(directory="public"), name="public")

# --- بيئة ---
# ✨ دعم تمرير اعتماد جوجل كـ JSON عبر متغير بيئة (GCP_KEY_JSON) وكتابته لملف gcp.json وقت التشغيل
GCP_KEY_JSON = os.getenv("GCP_KEY_JSON")
if GCP_KEY_JSON:
    try:
        with open("gcp.json", "w", encoding="utf-8") as f:
            f.write(GCP_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
    except Exception:
        pass
PORT = int(os.getenv("PORT", 5000))
BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
GCP_CRED = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

# --- قواعد البيانات ---
DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
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
  reply_audio_url TEXT
);
""")
conn.commit()
conn.close()

# --- خدمات خارجية ---
from twilio.rest import Client as TwilioClient
from openai import OpenAI
from google.cloud import speech_v1p1beta1 as speech

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# --- FastAPI ---


# تأكد من وجود مجلد tts
os.makedirs("public/tts", exist_ok=True)

# تخزين حالة المكالمات البسيط بالذاكرة (MVP)
CALL_STATE = {}
# CALL_STATE[call_sid] = {"turn": 0, "buffer": bytearray(), "stt": speech_client, ...}

# مُساعد: حفظ سجل

def log_conv(call_sid: str, turn: int, user_text: str, intent: str, tool_called: str, tool_result: str, reply_text: str, reply_audio_url: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO conversations (timestamp, call_sid, turn, user_text, intent, tool_called, tool_result, reply_text, reply_audio_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.datetime.utcnow().isoformat(), call_sid, turn, user_text, intent or "", tool_called or "", tool_result or "", reply_text or "", reply_audio_url or ""
        )
    )
    conn.commit()
    conn.close()

# 1) Twilio يطلب تعليمات البداية
@app.post("/voice")
async def voice(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")

    # جهز حالة المكالمة
    CALL_STATE[call_sid] = {"turn": 0}

    # TwiML: بدء بث Media Streams + تحية أولية قصيرة
    # نبني عنوان WebSocket من BASE_URL أو من المضيف الحالي
    from urllib.parse import urlparse
    ws_host = urlparse(BASE_URL).netloc or request.url.hostname
    wss_url = f"wss://{ws_host}/media?callSid={call_sid}"

    twiml = f"""
<Response>
  <Start>
    <Stream url="{wss_url}"/>
  </Start>
  <Say language="ar-SA" voice="Polly.Zeina">مرحبًا بكم في سمارت كول سنتر. تفضّل بالحديث، أنا أُصغي إليك.</Say>
  <Pause length="600"/>
</Response>
""".strip()
    return Response(content=twiml, media_type="text/xml; charset=utf-8")

# 2) نقطة الـWebSocket لاستقبال الصوت من Twilio Media Streams
@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    params = dict(ws.headers)
    # Twilio يرسل query string، نلتقط callSid من URL
    query = ws.scope.get("query_string", b"").decode()
    call_sid = "";
    if "callSid=" in query:
        call_sid = query.split("callSid=")[1].split("&")[0]

    # مُهيّئ Google STT (Streaming)
    speech_client = speech.SpeechClient()
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

    requests_gen = _stt_request_generator()
    stt_responses = speech_client.streaming_recognize(streaming_config, requests_gen)

    # نطلق مهمة قراءة استجابات STT بشكل غير متزامن
    import asyncio
    stt_task = asyncio.create_task(_consume_stt_responses(stt_responses, call_sid))

    try:
        while True:
            msg = await ws.receive_text()
            event = json.loads(msg)
            ev_type = event.get("event")

            if ev_type == "start":
                # بدء البث
                continue
            elif ev_type == "media":
                # الصوت مُرمّز base64 μ-law 8kHz
                b64 = event.get("media", {}).get("payload")
                if b64:
                    ulaw = base64.b64decode(b64)
                    # حول μ-law إلى Linear PCM 16-bit
                    pcm = audioop.ulaw2lin(ulaw, 2)
                    await requests_gen.asend(pcm)
            elif ev_type == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            await requests_gen.aclose()
        except Exception:
            pass
        await stt_task
        await ws.close()

# مولد طلبات STT (async generator)
async def _stt_request_generator():
    from google.cloud.speech_v1p1beta1 import StreamingRecognizeRequest
    try:
        chunk = yield
        while True:
            yield StreamingRecognizeRequest(audio_content=chunk)
            chunk = yield
    except GeneratorExit:
        return

# معالجة ردود STT
async def _consume_stt_responses(stt_responses, call_sid: str):
    import asyncio
    async for resp in _aiter(stt_responses):
        for result in resp.results:
            transcript = result.alternatives[0].transcript.strip()
            if result.is_final and transcript:
                # عندما نحصل على جملة «نهائية» نتعامل معها كدَوْر
                await _handle_user_turn(call_sid, transcript)

async def _aiter(stream):
    # محوّل بسيط لمولد gRPC إلى async
    loop = asyncio.get_event_loop()
    iterator = iter(stream)
    while True:
        try:
            item = await loop.run_in_executor(None, next, iterator)
        except StopIteration:
            break
        yield item

# معالجة دورة حوار واحدة
async def _handle_user_turn(call_sid: str, user_text: str):
    # 1) استنتاج النية والرد النصي + أدوات
    intent, tool_called, tool_result, reply_text = await _llm_plan_and_reply(user_text)

    # 2) تشكيل وتحسين الإلقاء
    prepared_text = await _arabic_diacritize_and_style(reply_text)

    # 3) توليد TTS (mp3) وحفظه
    mp3_url = await _synthesize_tts(prepared_text)

    # 4) تحديث مكالمة Twilio لِتشغيل الصوت ثم إعادة الاستماع
    if twilio_client:
        from twilio.base.exceptions import TwilioException
        try:
            if mp3_url:
                twiml = f"""
<Response>
  <Play>{mp3_url}</Play>
  <Redirect method="POST">/voice</Redirect>
</Response>
""".strip()
            else:
                # فallback مباشر: انطق النص عبر Twilio Say لو فشل توليد ملف الصوت
                safe_text = prepared_text or reply_text or "حسنًا، هل يمكنك التوضيح أكثر؟"
                twiml = f"""
<Response>
  <Say language=\"ar-SA\" voice=\"Polly.Zeina\">{safe_text}</Say>
  <Redirect method=\"POST\">/voice</Redirect>
</Response>
""".strip()
            twilio_client.calls(call_sid).update(twiml=twiml)
        except TwilioException:
            pass

    # 5) سجل
    state = CALL_STATE.get(call_sid, {"turn": 0})
    turn = state.get("turn", 0) + 1
    state["turn"] = turn
    CALL_STATE[call_sid] = state
    log_conv(call_sid, turn, user_text, intent, tool_called, tool_result, reply_text, mp3_url)

# (أ) LLM: تخطيط النية + أدوات
async def _llm_plan_and_reply(user_text: str):
    intent = None; tool_called=None; tool_result=None; answer = None
    if not openai_client:
        return intent, tool_called, tool_result, "من فضلك أعد طلبك لاحقاً."

    SYSTEM = (
        "أنت مساعد اتصال لشركة اتصالات سعودية. استنتج النية بإيجاز، واختر أداة واحدة إن لزم، ثم اكتب جواباً موجزاً مهذباً بالعربية الفصحى."
        " لا تتجاوز جملتين إلى ثلاث."
    )

    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_balance",
                "description": "جلب رصيد العميل الحالي باستخدام رقم هاتفه أو رقم حسابه.",
                "parameters": {
                    "type": "object",
                    "properties": {"customer_id": {"type": "string"}},
                    "required": ["customer_id"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "open_ticket",
                "description": "فتح تذكرة دعم فني مع وصف موجز للمشكلة.",
                "parameters": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"]
                }
            }
        }
    ]

    try:
        comp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_text}
            ],
            tools=tools,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=180,
        )
        choice = comp.choices[0]
        if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
            call = choice.message.tool_calls[0]
            tool_called = call.function.name
            args = json.loads(call.function.arguments or "{}")
            if tool_called == "lookup_balance":
                tool_result = _mock_lookup_balance(args.get("customer_id", ""))
                intent = "balance_inquiry"
                answer = f"رصيدك الحالي هو {tool_result.get('balance', 'غير متاح')} ريال."
            elif tool_called == "open_ticket":
                ticket_id = _mock_open_ticket(args.get("summary", ""))
                intent = "open_ticket"
                answer = f"تم فتح تذكرة رقم {ticket_id}. سنوافيك بالتحديثات."
        if not answer:
            # جواب مباشر بلا أدوات
            intent = intent or "general_support"
            answer = choice.message.content.strip()
    except Exception:
        answer = "عذراً، لم أفهم جيداً. هل يمكنك التوضيح؟"

    return intent, tool_called, (json.dumps(tool_result, ensure_ascii=False) if tool_result else None), answer

# الأدوات الوهمية (MVP)
def _mock_lookup_balance(customer_id: str):
    # هنا تربط لاحقاً بقاعدة بياناتك الفعلية
    return {"customer_id": customer_id or "12345", "balance": 150.50}

def _mock_open_ticket(summary: str):
    return f"T-{str(uuid.uuid4())[:8]}"

# (ب) تشكيل + أسلوب الإلقاء
async def _arabic_diacritize_and_style(text: str) -> str:
    if not openai_client:
        return text
    prompt = (
        "أضف التشكيل العربي للنص التالي بدقة وتهذيب، وأدرج إشارات توقف مناسبة (مثلاً: [pause=300ms]) دون إطالة مبالغ فيها.\n"
        "أعد النص مشروحاً بالتشكيل الكامل قدر الإمكان مع علامات ترقيم سليمة.\n\n"
        f"النص: {text}"
    )
    try:
        comp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=250,
        )
        return comp.choices[0].message.content.strip()
    except Exception:
        return text

# (ج) توليد TTS (ملف mp3 في public/tts) عبر OpenAI
async def _synthesize_tts(text: str) -> Optional[str]:
    if not openai_client:
        return None
    try:
        # بعض واجهات TTS تعيد بايتات الصوت مباشرة
        audio = openai_client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text,
            format="mp3"
        )
        audio_bytes = audio.read()
        file_id = f"{uuid.uuid4()}.mp3"
        path = os.path.join("public", "tts", file_id)
        with open(path, "wb") as f:
            f.write(audio_bytes)
        return f"{BASE_URL}/public/tts/{file_id}"
    except Exception:
        return None

# فحص سريع
@app.get("/health")
async def health():
    return PlainTextResponse("OK")

