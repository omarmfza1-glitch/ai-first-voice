import os, io, base64, uuid, sqlite3, datetime, json, asyncio, logging, queue
from typing import Optional, Dict, Any, Generator

# --- third-party ---
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

# Twilio + Google + OpenAI
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException
from google.cloud import speech_v1p1beta1 as speech
from google.cloud.speech_v1p1beta1 import StreamingRecognizeRequest, StreamingRecognitionConfig, RecognitionConfig
from openai import OpenAI

# audioop for μ-law → PCM16
try:
    import audioop
except ModuleNotFoundError:  # Python 3.13
    import audioop_lts as audioop

# =====================
# إعداد عام
# =====================
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger("smart-cc")

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

# SDKs
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
speech_client = speech.SpeechClient()
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None
log.info("✅ Google Cloud Speech Client initialized")

# =====================
# قاعدة البيانات (SQLite)
# =====================
DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
        reply_audio_url TEXT
    );
    """
)
conn.commit()
conn.close()
log.info("✅ Database initialized successfully.")

# =====================
# FastAPI
# =====================
app = FastAPI()
os.makedirs("public/tts", exist_ok=True)
app.mount("/public", StaticFiles(directory="public"), name="public")

# =====================
# حالة المكالمة في الذاكرة
# =====================
class CallState:
    def __init__(self) -> None:
        self.turn = 0
        self.frames = 0
        self.stream_sid: str = ""
        self.call_sid: str = ""
        self.req_queue: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self.req_task: Optional[asyncio.Task] = None
        self.resp_task: Optional[asyncio.Task] = None

CALLS: Dict[str, CallState] = {}


def log_conv(call_sid: str, turn: int, user_text: str, intent: str, tool_called: str, tool_result: str, reply_text: str, reply_audio_url: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO conversations (timestamp, call_sid, turn, user_text, intent, tool_called, tool_result, reply_text, reply_audio_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.datetime.utcnow().isoformat(), call_sid, turn, user_text, intent or "", tool_called or "", tool_result or "", reply_text or "", reply_audio_url or ""),
    )
    conn.commit()
    conn.close()

# =====================
# Google STT: نبني مُولِّد requests الصحيح
#   ملاحظة مهمة: لا نستخدم أي helpers. نمرّر iterable واحد فقط إلى streaming_recognize
# =====================

def make_streaming_config() -> StreamingRecognitionConfig:
    return StreamingRecognitionConfig(
        config=RecognitionConfig(
            encoding=RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="ar-SA",
            # مهم: لا نستخدم alternative_language_codes مع model=phone_call لأن Google تُرجِع 400
            use_enhanced=True,
            model="phone_call",
            enable_automatic_punctuation=True,
        ),
        interim_results=True,
        single_utterance=False,
    )


class RequestIterable:
    """Iterable يُنتِج الرسالة الأولى (streaming_config) ثم رسائل audio_content من Queue."""

    def __init__(self, q: "queue.Queue[Optional[bytes]]", streaming_config: StreamingRecognitionConfig):
        self.q = q
        self.streaming_config = streaming_config
        self._config_sent = False

    def __iter__(self):
        # أولاً: أرسل الـ config مرّة واحدة
        if not self._config_sent:
            self._config_sent = True
            yield StreamingRecognizeRequest(streaming_config=self.streaming_config)
        # ثم أغذّي الصوت
        while True:
            chunk = self.q.get()
            if chunk is None:
                break
            yield StreamingRecognizeRequest(audio_content=chunk)


async def consume_stt_responses(call_sid: str, responses) -> None:
    """قراءة ردود STT في thread منفصل، وتمرير النتائج النهائية إلى معالج الدور."""
    loop = asyncio.get_running_loop()

    def _reader():
        try:
            for resp in responses:
                for result in resp.results:
                    if not result.alternatives:
                        continue
                    text = result.alternatives[0].transcript.strip()
                    final = getattr(result, "is_final", False)
                    if final and text:
                        log.info("📝 STT FINAL [%s]: %s", call_sid, text)
                        # استدعاء الكوروتين من thread
                        asyncio.run_coroutine_threadsafe(handle_user_turn(call_sid, text), loop)
        except Exception as e:
            log.error("❌ STT responses loop error: %s", e)

    await asyncio.to_thread(_reader)

# =====================
# OpenAI LLM + TTS
# =====================
async def llm_plan_and_reply(user_text: str):
    intent = None
    tool_called = None
    tool_result = None
    answer = None

    if not openai_client:
        return intent, tool_called, tool_result, "عذرًا، الخدمة غير متاحة مؤقتًا."

    SYSTEM = (
        "أنت مساعد اتصال لشركة اتصالات سعودية. استنتج النية بإيجاز، واختر أداة واحدة إن لزم، ثم اكتب جواباً موجزاً مهذباً بالعربية الفصحى."
    )
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_balance",
                "description": "جلب رصيد العميل الحالي باستخدام رقم هاتفه أو رقم حسابه.",
                "parameters": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "open_ticket",
                "description": "فتح تذكرة دعم فني مع وصف موجز للمشكلة.",
                "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]},
            },
        },
    ]

    try:
        comp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_text}],
            tools=tools,
            tool_choice="auto",
            temperature=0.2,
            max_tokens=180,
        )
        choice = comp.choices[0]
        msg = choice.message
        if choice.finish_reason == "tool_calls" and msg.tool_calls:
            call = msg.tool_calls[0]
            tool_called = call.function.name
            args = json.loads(call.function.arguments or "{}")
            if tool_called == "lookup_balance":
                tr = {"customer_id": args.get("customer_id") or "12345", "balance": 150.50}
                tool_result = json.dumps(tr, ensure_ascii=False)
                intent = "balance_inquiry"
                answer = f"رصيدك الحالي هو {tr.get('balance', 'غير متاح')} ريال."
            elif tool_called == "open_ticket":
                tid = f"T-{str(uuid.uuid4())[:8]}"
                tool_result = tid
                intent = "open_ticket"
                answer = f"تم فتح تذكرة رقم {tid}. سنوافيك بالتحديثات."
        if not answer:
            intent = intent or "general_support"
            answer = (msg.content or "").strip() or "حسنًا، هل يمكنك التوضيح؟"
    except Exception as e:
        log.error("LLM error: %s", e)
        answer = "عذراً، لم أفهم جيداً. هل يمكنك التوضيح؟"

    return intent, tool_called, tool_result, answer


async def synthesize_tts(text: str) -> Optional[str]:
    if not openai_client:
        return None
    file_id = f"{uuid.uuid4()}.mp3"
    out_path = os.path.join("public", "tts", file_id)
    # BASE_URL يجب أن يكون https في الإنتاج
    base = BASE_URL.rstrip("/")
    if base.startswith("http://"):
        # Heroku سيعمل https تلقائياً في الغالب – نستبدل هنا إن لزم
        base = base.replace("http://", "https://", 1)
    url = f"{base}/public/tts/{file_id}"
    try:
        with openai_client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text,
            response_format="mp3",
        ) as resp:
            resp.stream_to_file(out_path)
        log.info("🔊 TTS OK -> %s", url)
        return url
    except TypeError:
        # fallback لإصدارات لا تدعم response_format
        with openai_client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text,
        ) as resp:
            resp.stream_to_file(out_path)
        log.info("🔊 TTS OK (fallback) -> %s", url)
        return url
    except Exception as e:
        log.error("❌ TTS error: %s", e)
        return None


async def handle_user_turn(call_sid: str, user_text: str) -> None:
    intent, tool_called, tool_result, reply_text = await llm_plan_and_reply(user_text)
    # يمكنك إضافة تهيئة/تشكيل عربي هنا إن رغبت
    mp3_url = await synthesize_tts(reply_text)

    if twilio_client:
        try:
            if mp3_url:
                twiml = f"""
<Response>
  <Play>{mp3_url}</Play>
  <Redirect method=\"POST\">/twilio/voice</Redirect>
</Response>
""".strip()
            else:
                safe_text = reply_text or "حسنًا، هل يمكنك التوضيح أكثر؟"
                twiml = f"""
<Response>
  <Say language=\"ar-SA\" voice=\"Polly.Zeina\">{safe_text}</Say>
  <Redirect method=\"POST\">/twilio/voice</Redirect>
</Response>
""".strip()
            twilio_client.calls(call_sid).update(twiml=twiml)
            log.info("📲 CALL UPDATE [%s]: played mp3=%s", call_sid, bool(mp3_url))
        except TwilioException as e:
            log.error("❌ Twilio update error [%s]: %s", call_sid, e)

    st = CALLS.get(call_sid)
    if st:
        st.turn += 1
    log_conv(call_sid, (st.turn if st else 0), user_text, intent or "", tool_called or "", tool_result or "", reply_text, mp3_url or "")

# =====================
# Twilio: Voice Webhook (TwiML)
# =====================
@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")

    # حافظ على حالة المكالمة
    if call_sid not in CALLS:
        CALLS[call_sid] = CallState()
        CALLS[call_sid].call_sid = call_sid
        first_turn = True
    else:
        first_turn = CALLS[call_sid].turn == 0

    # استخرج المضيف من BASE_URL لبناء wss
    from urllib.parse import urlparse

    parsed = urlparse(BASE_URL)
    host = parsed.netloc or request.url.netloc
    wss_url = f"wss://{host}/twilio/media"

    greet = """
  <Say language=\"ar-SA\" voice=\"Polly.Zeina\">مرحبًا بكم في سمارت كول سنتر. تفضّل بالحديث، أنا أُصغي إليك.</Say>
    """ if first_turn else ""

    twiml = f"""
<Response>
  {greet}
  <Start>
    <Stream url=\"{wss_url}\"/>
  </Start>
</Response>
""".strip()
    log.info("📞 Greeting handler: Welcoming call %s and redirecting to stream.", call_sid)
    return Response(content=twiml, media_type="text/xml; charset=utf-8")


# =====================
# Twilio: WebSocket Media Stream
# =====================
@app.websocket("/twilio/media")
async def twilio_media(ws: WebSocket):
    await ws.accept()
    log.info("connection open")

    # سننشئ الحالة عند استلام حدث start (يحمل callSid/streamSid)
    st: Optional[CallState] = None

    # مهام الخلفية: لا ننشئها إلا بعد start
    try:
        while True:
            msg = await ws.receive_text()
            event = json.loads(msg)
            etype = event.get("event")

            if etype == "start":
                start_info = event.get("start", {})
                call_sid = start_info.get("callSid") or start_info.get("streamSid") or ""
                stream_sid = start_info.get("streamSid", "")
                if call_sid not in CALLS:
                    CALLS[call_sid] = CallState()
                st = CALLS[call_sid]
                st.call_sid = call_sid
                st.stream_sid = stream_sid
                log.info("▶️ WS Receiver: Stream started for call: %s", call_sid)

                # إعداد الـ STT
                streaming_config = make_streaming_config()
                req_iterable = RequestIterable(st.req_queue, streaming_config)
                responses = speech_client.streaming_recognize(req_iterable)  # مهم: نمرّر iterable واحد فقط
                st.resp_task = asyncio.create_task(consume_stt_responses(call_sid, responses))

            elif etype == "media":
                if not st:
                    continue  # لم يصل start بعد
                b64 = event.get("media", {}).get("payload")
                if b64:
                    ulaw = base64.b64decode(b64)
                    pcm16 = audioop.ulaw2lin(ulaw, 2)  # 16-bit PCM
                    st.req_queue.put(pcm16)
                    st.frames += 1
                    if st.frames in (1, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100):
                        log.info("🎙️ Media frames forwarded to STT for %s: %d", st.call_sid, st.frames)

            elif etype == "mark":
                pass

            elif etype == "stop":
                log.info("⏹️ WS Receiver: Stream stopped. Ending queue.")
                if st:
                    st.req_queue.put(None)
                break

    except WebSocketDisconnect:
        log.info("WS disconnect")
    except Exception as e:
        log.error("WS error: %s", e)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
        log.info("connection closed")


# =====================
# Twilio: Status Callback
# =====================
@app.post("/twilio/status")
async def twilio_status(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    log.info("📞 Call Status: %s -> %s", call_sid, call_status)

    if call_status in {"completed", "canceled", "failed", "busy", "no-answer"}:
        if call_sid in CALLS:
            CALLS.pop(call_sid, None)
            log.info("🧹 Cleaned up state for call %s", call_sid)
    return PlainTextResponse("")


# =====================
# Healthcheck
# =====================
@app.get("/health")
async def health():
    return PlainTextResponse("OK")


# =====================
# Local run
# =====================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
