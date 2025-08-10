# app.py
import os, base64, uuid, sqlite3, datetime, json, asyncio, logging, threading, queue, time
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

# ================== البيئة ==================
load_dotenv()

# خيار 1: تمرير اعتماد Google كـ JSON في متغير GCP_KEY_JSON (مريح لهيروكو)
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

# ================== قاعدة البيانات ==================
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

# ================== عملاء الخدمات ==================
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException
from openai import OpenAI
from google.cloud import speech_v1p1beta1 as speech

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None

# ================== تطبيق FastAPI ==================
app = FastAPI()
os.makedirs("public/tts", exist_ok=True)
app.mount("/public", StaticFiles(directory="public"), name="public")

# حالة المكالمات بالذاكرة (MVP)
# state = { call_sid: { "turn": int, "awaiting_reply": bool, "last_emit_ts": float } }
CALL_STATE = {}

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

# ================== مسارات مساعدة ==================
@app.get("/health")
async def health():
    return PlainTextResponse("OK")

@app.post("/twilio/status")
async def twilio_status(request: Request):
    # لتجنّب 404 في سجلات Twilio
    return PlainTextResponse("OK")

# ================== بدء المكالمة: /voice ==================
@app.post("/voice")
async def voice(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")

    # حافظ على الحالة ولا تعيد تهيئتها إذا موجودة — لتجنّب تكرار التحية
    state = CALL_STATE.get(call_sid)
    if state is None:
        CALL_STATE[call_sid] = {"turn": 0, "awaiting_reply": False, "last_emit_ts": 0.0}
        is_first_turn = True
    else:
        # وصلنا هنا عبر <Redirect> بعد ردّ سابق — اسمح بدور جديد (أعد الاستماع)
        state["awaiting_reply"] = False
        is_first_turn = (state.get("turn", 0) == 0)

    # ابنِ عنوان WebSocket من BASE_URL أو من المضيف الحالي
    parsed = urlparse(BASE_URL)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_host = parsed.netloc or request.url.hostname
    wss_url = f"{ws_scheme}://{ws_host}/media?callSid={call_sid}"

    if is_first_turn:
        # تحية أول مرة فقط
        twiml = f"""
<Response>
  <Start><Stream url="{wss_url}"/></Start>
  <Say language="ar-SA" voice="Polly.Zeina">مرحبًا بكم في سمارت كول سنتر. تفضّل بالحديث، أنا أُصغي إليك.</Say>
  <Pause length="60"/>
</Response>
""".strip()
    else:
        # الأدوار اللاحقة: افتح الاستماع فقط بدون تحية
        twiml = f"""
<Response>
  <Start><Stream url="{wss_url}"/></Start>
  <Pause length="60"/>
</Response>
""".strip()

    log.info(f"/voice: started call_sid={call_sid} -> stream {wss_url} (first_turn={is_first_turn})")
    return Response(content=twiml, media_type="text/xml; charset=utf-8")

# ================== WebSocket للاستماع ==================
@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()

    qs = ws.scope.get("query_string", b"").decode()
    qd = parse_qs(qs)
    call_sid = (qd.get("CallSid") or qd.get("callSid") or [""])[0]
    log.info(f"WS connected: call_sid={call_sid}")

    # إعداد Google STT — أكثر إعداد آمن للعربية عبر الهاتف
    speech_client = speech.SpeechClient()
    recognition_config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=8000,
        language_code="ar-SA",
        model="default",                     # phone_call لا يدعم ar-SA في حسابات كثيرة
        enable_automatic_punctuation=True,
    )
    streaming_config = speech.StreamingRecognitionConfig(
        config=recognition_config,
        interim_results=True,
        single_utterance=False,              # True غير مدعوم/يسبب إغلاق مبكر
    )

    # طابور لتجميع الصوت وإرساله إلى Google
    audio_q: queue.Queue[bytes] = queue.Queue(maxsize=400)

    def request_iter():
        from google.cloud.speech_v1p1beta1 import StreamingRecognizeRequest
        while True:
            chunk = audio_q.get()
            if chunk is None:
                break
            yield StreamingRecognizeRequest(audio_content=chunk)

    loop = asyncio.get_event_loop()

    # مستهلك ردود STT في خيط منفصل، ويُطلق الردود مبكرًا على نتائج مستقرة
    def stt_consumer():
        try:
            last_sent_text = ""
            min_emit_gap = 1.5  # ثواني بين إطلاقين
            for resp in speech_client.streaming_recognize(streaming_config, request_iter()):
                for result in resp.results:
                    alt = result.alternatives[0]
                    transcript = (alt.transcript or "").strip()
                    if not transcript:
                        continue

                    state = CALL_STATE.get(call_sid, {})
                    # لو عندنا رد قيد الإرسال، تجاهل أي transcriptions حتى يُعاد فتح /voice
                    if state.get("awaiting_reply"):
                        continue

                    now = time.time()
                    emit = False
                    if result.is_final:
                        emit = True
                    else:
                        stab = getattr(result, "stability", 0.0) or 0.0
                        if stab >= 0.85 or len(transcript) >= 14:
                            if transcript != last_sent_text and (now - state.get("last_emit_ts", 0.0) > min_emit_gap):
                                emit = True

                    if emit:
                        last_sent_text = transcript
                        state["awaiting_reply"] = True
                        state["last_emit_ts"] = now
                        CALL_STATE[call_sid] = state
                        log.info(f"STT FINAL* [{call_sid}]: {transcript}")
                        asyncio.run_coroutine_threadsafe(_handle_user_turn(call_sid, transcript), loop)
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
                start = event.get("start", {}) or event.get("Start", {})
                ev_call_sid = start.get("callSid") or start.get("CallSid")
                if ev_call_sid:
                    call_sid = ev_call_sid
                    log.info(f"WS start: call_sid={call_sid}")

            elif et == "media":
                b64 = event.get("media", {}).get("payload")
                if b64:
                    ulaw = base64.b64decode(b64)
                    pcm = audioop.ulaw2lin(ulaw, 2)  # μ-law → PCM16
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

# ================== منطق الحوار ==================
async def _handle_user_turn(call_sid: str, user_text: str):
    log.info(f"HANDLE TURN [{call_sid}]: '{user_text}'")

    # 1) استنتاج النية + الرد
    intent, tool_called, tool_result, reply_text = await _llm_plan_and_reply(user_text)

    # 2) تشكيل عربي وتحسين الإلقاء
    prepared_text = await _arabic_diacritize_and_style(reply_text)

    # 3) توليد TTS إلى mp3
    mp3_url = await _synthesize_tts(prepared_text)

    # 4) إرسال الرد فورًا عبر تحديث المكالمة (طالما in-progress)
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

    # 5) تحديث الحالة + السجل
    state = CALL_STATE.get(call_sid, {"turn": 0})
    state["turn"] = state.get("turn", 0) + 1
    # ننتظر Redirect ليعيد فتح /voice ثم نجعل awaiting_reply=False هناك
    state["awaiting_reply"] = True
    state["last_emit_ts"] = time.time()
    CALL_STATE[call_sid] = state
    log_conv(call_sid, state["turn"], user_text, intent, tool_called, tool_result, reply_text, mp3_url or "")

async def _llm_plan_and_reply(user_text: str):
    intent = tool_called = tool_result = None
    answer = None
    if not openai_client:
        return intent, tool_called, tool_result, "من فضلك أعد طلبك لاحقاً."

    SYSTEM = (
        "أنت مساعد اتصال لشركة اتصالات سعودية. استنتج نية المتصل بإيجاز، واختر أداة واحدة إن لزم، "
        "ثم اكتب جوابًا مهذبًا بالعربية الفصحى لا يتجاوز ثلاث جمل."
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

# أدوات وهمية (اربطها لاحقًا بقاعدة بياناتك/خدماتك)
def _mock_lookup_balance(customer_id: str):
    return {"customer_id": customer_id or "12345", "balance": 150.50}

def _mock_open_ticket(summary: str):
    return f"T-{str(uuid.uuid4())[:8]}"

# تشكيل عربي + أسلوب إلقاء
async def _arabic_diacritize_and_style(text: str) -> str:
    if not openai_client:
        return text
    prompt = (
        "أضف التشكيل العربي للنص التالي بدقّة وتهذيب، مع فواصل طبيعية (مثل: [pause=300ms]) "
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

# TTS إلى ملف mp3 في public/tts
async def _synthesize_tts(text: str) -> Optional[str]:
    if not openai_client:
        return None
    try:
        # لا نستخدم with_streaming_response لتجنّب مشاكل التوافق
        resp = openai_client.audio.speech.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text,
            format="mp3"  # هذه مدعومة في الإصدارات الحديثة من بايثون/المكتبة لديك (حسب سجلاتك 200 OK)
        )
        # استخراج البايتات
        audio_bytes = None
        if hasattr(resp, "read"):
            audio_bytes = resp.read()
        elif hasattr(resp, "content"):
            audio_bytes = resp.content
        else:
            try:
                audio_bytes = b"".join(resp.iter_bytes())
            except Exception:
                pass

        if not audio_bytes:
            log.error("TTS: empty audio bytes")
            return None

        file_id = f"{uuid.uuid4()}.mp3"
        path = os.path.join("public", "tts", file_id)
        with open(path, "wb") as f:
            f.write(audio_bytes)

        return f"{BASE_URL}/public/tts/{file_id}"
    except Exception as e:
        log.exception(f"TTS error: {e}")
        return None
