# app.py
import os, base64, uuid, sqlite3, datetime, json, asyncio, logging, threading, queue
from urllib.parse import urlparse, parse_qs
from typing import Optional

# دعم audioop على بايثون 3.13
try:
    import audioop          # Python <= 3.12
except ModuleNotFoundError:
    import audioop_lts as audioop  # بديل متوافق

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("smart-cc")

# ====== البيئة ======
load_dotenv()

# اكتب اعتماد Google من المتغير GCP_KEY_JSON إلى ملف gcp.json
GCP_KEY_JSON = os.getenv("GCP_KEY_JSON")
if GCP_KEY_JSON:
    try:
        with open("gcp.json", "w", encoding="utf-8") as f:
            f.write(GCP_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
        log.info("GCP credentials written to gcp.json")
    except Exception as e:
        log.exception(f"Failed writing gcp.json: {e}")

PORT = int(os.getenv("PORT", 5000))
BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}").rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN  = os.getenv("TWILIO_AUTH_TOKEN")

# ====== قواعد البيانات (SQLite) ======
DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)

def init_db():
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
init_db()

# ====== عملاء الخدمات ======
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException
from openai import OpenAI
from google.cloud import speech_v1p1beta1 as speech

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None

# ====== تطبيق FastAPI + الملفات الثابتة ======
app = FastAPI()
os.makedirs("public/tts", exist_ok=True)           # ينشئ public أيضًا إن لم يوجد
app.mount("/public", StaticFiles(directory="public"), name="public")

CALL_STATE = {}  # حالة المكالمات بالذاكرة

def log_conv(call_sid: str, turn: int, user_text: str, intent: str,
             tool_called: str, tool_result: str, reply_text: str, reply_audio_url: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO conversations (timestamp, call_sid, turn, user_text, intent, tool_called, tool_result, reply_text, reply_audio_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.datetime.utcnow().isoformat(), call_sid, turn, user_text or "", intent or "",
         tool_called or "", tool_result or "", reply_text or "", reply_audio_url or "")
    )
    conn.commit()
    conn.close()

# ================== المسارات ==================
@app.get("/health")
async def health():
    return PlainTextResponse("OK")

# (اختياري) لمنع 404 من Twilio Status Callback إن وُضع
@app.post("/twilio/status")
async def twilio_status(request: Request):
    return PlainTextResponse("OK")

# بدء المكالمة: تحية + تفعيل Media Streams
@app.post("/voice")
async def voice(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    CALL_STATE[call_sid] = {"turn": 0}

    parsed = urlparse(BASE_URL)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_host = parsed.netloc or request.url.hostname
    wss_url = f"{ws_scheme}://{ws_host}/media?callSid={call_sid}"

    twiml = f"""
<Response>
  <Start><Stream url="{wss_url}"/></Start>
  <Say language="ar-SA" voice="Polly.Zeina">مرحبًا بكم في سمارت كول سنتر. تفضّل بالحديث، أنا أُصغي إليك.</Say>
  <Pause length="30"/>
</Response>
""".strip()
    log.info(f"/voice: started call_sid={call_sid} -> stream {wss_url}")
    return Response(content=twiml, media_type="text/xml; charset=utf-8")

# WebSocket: يستقبل صوت μ-law 8kHz من Twilio → Google STT (Streaming)
@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()

    # جرّب أخذ callSid من الـquery (قد يكون فارغ حتى يصل حدث start)
    qs = ws.scope.get("query_string", b"").decode()
    qd = parse_qs(qs)
    call_sid = (qd.get("CallSid") or qd.get("callSid") or [""])[0]
    log.info(f"WS connected: call_sid={call_sid}")

    # إعداد Google STT (العربية لا تدعم model=phone_call)
    speech_client = speech.SpeechClient()
    recognition_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=8000,
        language_code="ar-SA",
        model="default",                      # ← مهم
        enable_automatic_punctuation=True,
        # use_enhanced غير مدعومة هنا
    )
    streaming_config = speech.StreamingRecognitionConfig(
        config=recognition_config,
        interim_results=True,
        single_utterance=True,                # ← يُنهي الجملة سريعًا عند الوقفة
    )

    # Queue + مولّد: يرسل الصوت فقط (نسخة المكتبة هذه تتوقع config كوسيط منفصل)
    audio_q: queue.Queue[bytes] = queue.Queue(maxsize=200)

    def request_iter():
        from google.cloud.speech_v1p1beta1 import StreamingRecognizeRequest
        while True:
            chunk = audio_q.get()
            if chunk is None:
                break
            yield StreamingRecognizeRequest(audio_content=chunk)

    loop = asyncio.get_event_loop()

    def stt_consumer():
        try:
            # نمرر streaming_config + المولّد (requests)
            for resp in speech_client.streaming_recognize(streaming_config, request_iter()):
                for result in resp.results:
                    if result.is_final:
                        transcript = result.alternatives[0].transcript.strip()
                        if transcript:
                            log.info(f"STT FINAL [{call_sid}]: {transcript}")
                            asyncio.run_coroutine_threadsafe(
                                _handle_user_turn(call_sid, transcript), loop
                            )
        except Exception as e:
            log.exception(f"STT thread error: {e}")

    t = threading.Thread(target=stt_consumer, daemon=True)
    t.start()

    try:
        while True:
            msg = await ws.receive_text()
            event = json.loads(msg)
            et = event.get("event")

            if et == "start":
                # هنا نحصل على callSid المؤكد من حدث start
                start = event.get("start", {}) or event.get("Start", {})
                ev_call_sid = start.get("callSid") or start.get("CallSid")
                if ev_call_sid:
                    call_sid = ev_call_sid
                    log.info(f"WS start: call_sid={call_sid}")

            elif et == "media":
                b64 = event.get("media", {}).get("payload")
                if b64:
                    # μ-law → PCM16
                    ulaw = base64.b64decode(b64)
                    pcm = audioop.ulaw2lin(ulaw, 2)
                    try:
                        audio_q.put_nowait(pcm)
                    except queue.Full:
                        _ = audio_q.get_nowait()
                        audio_q.put_nowait(pcm)

            elif et == "stop":
                log.info(f"WS stop [{call_sid}]")
                break

    except WebSocketDisconnect:
        log.info(f"WS disconnected [{call_sid}]")
    except Exception as e:
        log.exception(f"WS error [{call_sid}]: {e}")
    finally:
        try:
            audio_q.put(None)
        except Exception:
            pass
        t.join(timeout=1)
        await ws.close()

# ====== منطق الحوار ======
async def _handle_user_turn(call_sid: str, user_text: str):
    log.info(f"HANDLE TURN [{call_sid}]: '{user_text}'")

    intent, tool_called, tool_result, reply_text = await _llm_plan_and_reply(user_text)
    prepared_text = await _arabic_diacritize_and_style(reply_text)
    mp3_url = await _synthesize_tts(prepared_text)

    if twilio_client and call_sid:
        try:
            if mp3_url:
                twiml = f"""
<Response>
  <Play>{mp3_url}</Play>
  <Redirect method="POST">/voice</Redirect>
</Response>
""".strip()
            else:
                safe_text = prepared_text or reply_text or "حسنًا، هل يمكنك التوضيح أكثر؟"
                twiml = f"""
<Response>
  <Say language="ar-SA" voice="Polly.Zeina">{safe_text}</Say>
  <Redirect method="POST">/voice</Redirect>
</Response>
""".strip()
            twilio_client.calls(call_sid).update(twiml=twiml)
            log.info(f"CALL UPDATE [{call_sid}]: played mp3={bool(mp3_url)}")
        except TwilioException as e:
            log.exception(f"Twilio update error [{call_sid}]: {e}")

    state = CALL_STATE.get(call_sid, {"turn": 0})
    state["turn"] = state.get("turn", 0) + 1
    CALL_STATE[call_sid] = state
    log_conv(call_sid, state["turn"], user_text, intent, tool_called, tool_result, reply_text, mp3_url or "")

async def _llm_plan_and_reply(user_text: str):
    intent = tool_called = tool_result = None
    answer = None
    if not openai_client:
        return intent, tool_called, tool_result, "من فضلك أعد طلبك لاحقاً."

    SYSTEM = (
        "أنت مساعد اتصال لشركة اتصالات سعودية. استنتج النية بإيجاز، واختر أداة واحدة إن لزم، "
        "ثم اكتب جواباً موجزاً مهذباً بالعربية الفصحى لا يتجاوز ثلاث جُمل."
    )

    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_balance",
                "description": "جلب رصيد العميل الحالي باستخدام رقم العميل.",
                "parameters": {"type": "object","properties":{"customer_id":{"type":"string"}},"required":["customer_id"]}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "open_ticket",
                "description": "فتح تذكرة دعم فني مع وصف موجز للمشكلة.",
                "parameters": {"type":"object","properties":{"summary":{"type":"string"}},"required":["summary"]}
            }
        }
    ]

    try:
        comp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":SYSTEM},{"role":"user","content":user_text}],
            tools=tools,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=180,
        )
        ch = comp.choices[0]
        if ch.finish_reason == "tool_calls" and ch.message.tool_calls:
            call = ch.message.tool_calls[0]
            tool_called = call.function.name
            args = json.loads(call.function.arguments or "{}")
            if tool_called == "lookup_balance":
                tool_result = _mock_lookup_balance(args.get("customer_id", ""))
                intent = "balance_inquiry"
                answer = f"رصيدك الحالي هو {tool_result.get('balance','غير متاح')} ريال."
            elif tool_called == "open_ticket":
                ticket_id = _mock_open_ticket(args.get("summary", ""))
                intent = "open_ticket"
                answer = f"تم فتح تذكرة رقم {ticket_id}. سنوافيك بالتحديثات."
        if not answer:
            intent = intent or "general_support"
            answer = ch.message.content.strip()
    except Exception as e:
        log.exception(f"LLM error: {e}")
        answer = "عذرًا، لم أفهم جيدًا. هل يمكنك التوضيح؟"

    return intent, tool_called, (json.dumps(tool_result, ensure_ascii=False) if tool_result else None), answer

# أدوات وهمية (اربطها لاحقًا ببياناتك)
def _mock_lookup_balance(customer_id: str):
    return {"customer_id": customer_id or "12345", "balance": 150.50}

def _mock_open_ticket(summary: str):
    return f"T-{str(uuid.uuid4())[:8]}"

# تشكيل + أسلوب الإلقاء
async def _arabic_diacritize_and_style(text: str) -> str:
    if not openai_client:
        return text
    prompt = (
        "أضف التشكيل العربي للنص التالي بدقة وتهذيب، مع فواصل طبيعية (مثل: [pause=300ms]) "
        "ودون إطالة مبالغ فيها. أعد النص مشكولًا مع علامات ترقيم سليمة.\n\n"
        f"{text}"
    )
    try:
        comp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=250,
        )
        return comp.choices[0].message.content.strip()
    except Exception as e:
        log.exception(f"Diacritize error: {e}")
        return text

# توليد TTS إلى ملف mp3 (بدون معامل format لتوافق المكتبة)
async def _synthesize_tts(text: str) -> Optional[str]:
    if not openai_client:
        return None
    try:
        file_id = f"{uuid.uuid4()}.mp3"
        path = os.path.join("public", "tts", file_id)

        # التوقيع المتوافق حاليًا: voice + input فقط
        with openai_client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text,
        ) as resp:
            resp.stream_to_file(path)

        return f"{BASE_URL}/public/tts/{file_id}"
    except Exception as e:
        log.exception(f"TTS error: {e}")
        return None
