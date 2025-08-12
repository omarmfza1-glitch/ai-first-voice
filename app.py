import os, io, base64, uuid, sqlite3, datetime, json, asyncio, logging, queue, threading
# دعم audioop على بايثون 3.13 عبر مكتبة بديلة
try:
    import audioop  # Python <= 3.12
except ModuleNotFoundError:
    import audioop_lts as audioop  # بديل متوافق

from typing import Optional
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

# خارجيات
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException
from openai import OpenAI
from google.cloud import speech_v1p1beta1 as speech
from google.cloud.speech_v1p1beta1 import StreamingRecognizeRequest

# ===================== إعداد عام =====================
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("smart-cc")

PORT = int(os.getenv("PORT", 5000))
BASE_URL = os.getenv("BASE_URL")  # يفضَّل تعيينه إلى https://<app>.herokuapp.com
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
GCP_KEY_JSON = os.getenv("GCP_KEY_JSON")

if GCP_KEY_JSON:
    try:
        with open("gcp.json", "w", encoding="utf-8") as f:
            f.write(GCP_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
        logger.info("✅ GCP credentials written to gcp.json")
    except Exception as e:
        logger.error("Failed to write gcp.json: %s", e)

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
speech_client = speech.SpeechClient()
logger.info("✅ Google Cloud Speech Client initialized")

twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None

# قاعدة بيانات صغيرة اختيارية
DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
with sqlite3.connect(DB_PATH) as _conn:
    _cur = _conn.cursor()
    _cur.execute(
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
    _conn.commit()
logger.info("✅ Database initialized successfully.")

# ===================== FastAPI =====================
app = FastAPI()
os.makedirs("public/tts", exist_ok=True)
app.mount("/public", StaticFiles(directory="public"), name="public")

CALL_STATE: dict[str, dict] = {}


def log_conv(call_sid: str, turn: int, user_text: str, intent: str, tool_called: str, tool_result: str, reply_text: str, reply_audio_url: str):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute(
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
            conn.commit()
    except Exception as e:
        logger.error("DB log error: %s", e)

# ===================== Twilio: IVR =====================
@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    CALL_STATE.setdefault(call_sid, {"turn": 0})

    # تحديد المضيف للـ WSS
    host = None
    if BASE_URL:
        try:
            from urllib.parse import urlparse
            host = urlparse(BASE_URL).netloc
        except Exception:
            host = None
    if not host:
        host = request.headers.get("Host") or request.url.netloc

    wss_url = f"wss://{host}/twilio/media"

    twiml = f"""
<Response>
  <Start>
    <Stream url="{wss_url}" />
  </Start>
  <Say language="ar-SA" voice="Polly.Zeina">مرحبًا بك في مركز الاتصال الذكي. تحدَّث من فضلك وأنا أستمع.</Say>
  <Pause length="60"/>
</Response>
""".strip()
    logger.info("📞 Greeting handler: Welcoming call %s and redirecting to stream.", call_sid)
    return Response(content=twiml, media_type="text/xml; charset=utf-8")

@app.post("/twilio/status")
async def twilio_status(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    status = form.get("CallStatus", "")
    logger.info("📞 Call Status: %s -> %s", call_sid, status)
    if status in {"completed", "canceled", "failed", "no-answer", "busy"}:
        CALL_STATE.pop(call_sid, None)
        logger.info("🧹 Cleaned up state for call %s", call_sid)
    return Response(content=b"", media_type="text/plain")

# ===================== Google STT: طلبات متدفقة صحيحة =====================
class STTStream:
    """مولِّد طلبات متوافق مع واجهة gRPC المنخفضة المستوى.
    أول عنصر: StreamingRecognizeRequest(streaming_config=...)
    العناصر التالية: StreamingRecognizeRequest(audio_content=...)
    """

    def __init__(self, streaming_config: speech.StreamingRecognitionConfig):
        self._q: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._closed = False
        self._streaming_config = streaming_config

    def push(self, pcm16: bytes):
        if not self._closed:
            self._q.put(pcm16)

    def close(self):
        if not self._closed:
            self._closed = True
            self._q.put(None)

    def __iter__(self):
        # أول رسالة = الكونفيج
        yield StreamingRecognizeRequest(streaming_config=self._streaming_config)
        # بعدها الصوت فقط
        while True:
            chunk = self._q.get()
            if chunk is None:
                break
            yield StreamingRecognizeRequest(audio_content=chunk)


# ===================== WebSocket للوسائط من Twilio =====================
@app.websocket("/twilio/media")
async def twilio_media(ws: WebSocket):
    await ws.accept()
    logger.info("connection open")

    # تكوين STT (بدون alternative_language_codes لأنها غير مدعومة لنموذج phone_call)
    streaming_config = speech.StreamingRecognitionConfig(
        config=speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="ar-SA",
            model="phone_call",
            use_enhanced=True,
            enable_automatic_punctuation=True,
        ),
        interim_results=True,
        single_utterance=False,
    )

    stt_stream: Optional[STTStream] = None
    stt_thread: Optional[threading.Thread] = None
    call_sid: Optional[str] = None

    loop = asyncio.get_running_loop()

    def _consume_stt_sync():
        try:
            # استخدام الواجهة المنخفضة: تمُرِّر requests فقط. لا helpers.
            responses = speech_client.streaming_recognize(requests=iter(stt_stream))  # type: ignore[arg-type]
            for resp in responses:
                for result in resp.results:
                    if result.is_final and result.alternatives:
                        transcript = (result.alternatives[0].transcript or "").strip()
                        if transcript:
                            logger.info("📝 STT FINAL [%s]: %s", call_sid or "?", transcript)
                            asyncio.run_coroutine_threadsafe(_handle_user_turn(call_sid or "", transcript), loop)
        except Exception as e:
            logger.error("❌ STT responses loop error: %s", e)

    frames = 0
    try:
        while True:
            try:
                msg = await ws.receive_text()
            except WebSocketDisconnect:
                break

            event = json.loads(msg)
            etype = event.get("event")

            if etype == "start":
                call_sid = (event.get("start") or {}).get("callSid") or ""
                logger.info("▶️ WS Receiver: Stream started for call: %s", call_sid)
                stt_stream = STTStream(streaming_config)
                stt_thread = threading.Thread(target=_consume_stt_sync, daemon=True)
                stt_thread.start()

            elif etype == "media":
                # استلام audio/ulaw 8kHz من Twilio وتحويله إلى PCM16
                b64 = (event.get("media") or {}).get("payload")
                if b64 and stt_stream is not None:
                    ulaw = base64.b64decode(b64)
                    pcm16 = audioop.ulaw2lin(ulaw, 2)  # 16-bit
                    stt_stream.push(pcm16)
                    frames += 1
                    if frames in (1, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000):
                        logger.info("🎙️ Media frames forwarded to STT for %s: %d", call_sid or "?", frames)

            elif etype == "mark":
                # يمكن تجاهلها
                pass

            elif etype == "stop":
                logger.info("⏹️ WS Receiver: Stream stopped. Ending queue.")
                if stt_stream is not None:
                    stt_stream.close()
                break

    except Exception as e:
        logger.exception("WS error: %s", e)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
        logger.info("connection closed")

# ===================== LLM + TTS =====================
from openai.types.chat import ChatCompletionMessageToolCall

async def _llm_plan_and_reply(user_text: str):
    intent = None; tool_called=None; tool_result=None; answer=None
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
            call: ChatCompletionMessageToolCall = msg.tool_calls[0]
            tool_called = call.function.name
            args = json.loads(call.function.arguments or "{}")
            if tool_called == "lookup_balance":
                tr = _mock_lookup_balance(args.get("customer_id", ""))
                tool_result = json.dumps(tr, ensure_ascii=False)
                intent = "balance_inquiry"
                answer = f"رصيدك الحالي هو {tr.get('balance', 'غير متاح')} ريال."
            elif tool_called == "open_ticket":
                tid = _mock_open_ticket(args.get("summary", ""))
                tool_result = tid
                intent = "open_ticket"
                answer = f"تم فتح تذكرة رقم {tid}. سنوافيك بالتحديثات."
        if not answer:
            intent = intent or "general_support"
            answer = (msg.content or "").strip() or "حاضر، كيف أستطيع مساعدتك؟"
    except Exception as e:
        logger.error("LLM error: %s", e)
        answer = "عذراً، لم أفهم جيداً. هل يمكنك التوضيح؟"

    return intent, tool_called, tool_result, answer


def _mock_lookup_balance(customer_id: str):
    return {"customer_id": customer_id or "12345", "balance": 150.50}


def _mock_open_ticket(summary: str):
    return f"T-{str(uuid.uuid4())[:8]}"


async def _arabic_diacritize_and_style(text: str) -> str:
    if not openai_client:
        return text
    prompt = f"""أضف التشكيل العربي للنص التالي بدقة وتهذيب، وأدرج إشارات توقف مناسبة مثل [pause=300ms] دون مبالغة.
النص: {text}
"""
    try:
        comp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=250,
        )
        return (comp.choices[0].message.content or text).strip()
    except Exception:
        return text


async def _synthesize_tts(text: str) -> Optional[str]:
    if not openai_client:
        return None
    file_id = f"{uuid.uuid4()}.mp3"
    out_path = os.path.join("public", "tts", file_id)

    # حدِّث BASE_URL تلقائياً إن لم يكن مضبوطاً
    base = BASE_URL or ""
    if not base:
        # سيُستبدل عند الرد عبر Twilio، الاستخدام الأساس هنا هو المسار المحلي
        pass
    try:
        with openai_client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text,
            response_format="mp3",
        ) as resp:
            resp.stream_to_file(out_path)
        logger.info("TTS OK -> %s", out_path)
        # Twilio يحتاج URL مطلق
        host = base
        if not host:
            # احتياطي: استخدم متغير بيئة BASE_URL؛ يجب تعيينه لبيئة الإنتاج
            raise RuntimeError("BASE_URL is not set; set it to your public https URL")
        if host.endswith("/"):
            host = host[:-1]
        return f"{host}/public/tts/{file_id}"
    except TypeError:
        # fallback لإصدارات لا تدعم response_format
        with openai_client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text,
        ) as resp:
            resp.stream_to_file(out_path)
        logger.info("TTS OK (fallback) -> %s", out_path)
        host = BASE_URL.rstrip("/") if BASE_URL else ""
        if not host:
            raise RuntimeError("BASE_URL is not set; set it to your public https URL")
        return f"{host}/public/tts/{file_id}"
    except Exception as e:
        logger.error("TTS error: %s", e)
        return None


async def _handle_user_turn(call_sid: str, user_text: str):
    if not call_sid:
        logger.warning("No call_sid on user turn; skipping Twilio update.")
        return

    state = CALL_STATE.setdefault(call_sid, {"turn": 0})

    intent, tool_called, tool_result, reply_text = await _llm_plan_and_reply(user_text)
    prepared_text = await _arabic_diacritize_and_style(reply_text)
    try:
        mp3_url = await _synthesize_tts(prepared_text)
    except Exception as e:
        logger.error("TTS synth failed: %s", e)
        mp3_url = None

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
                safe_text = prepared_text or reply_text or "حسنًا، هل يمكنك التوضيح أكثر؟"
                twiml = f"""
<Response>
  <Say language=\"ar-SA\" voice=\"Polly.Zeina\">{safe_text}</Say>
  <Redirect method=\"POST\">/twilio/voice</Redirect>
</Response>
""".strip()
            twilio_client.calls(call_sid).update(twiml=twiml)
            logger.info("CALL UPDATE [%s]: played mp3=%s", call_sid, bool(mp3_url))
        except TwilioException as e:
            logger.error("Twilio update error [%s]: %s", call_sid, e)

    state["turn"] = state.get("turn", 0) + 1
    CALL_STATE[call_sid] = state
    log_conv(call_sid, state["turn"], user_text, intent or "", tool_called or "", tool_result or "", reply_text or "", mp3_url or "")


@app.get("/health")
async def health():
    return PlainTextResponse("OK")


if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 Starting Smart Call Center...")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
