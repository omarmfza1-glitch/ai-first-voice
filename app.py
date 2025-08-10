# app.py — V3
import os, io, base64, uuid, sqlite3, datetime, json, asyncio, logging, queue
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

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smart-cc")

# --- بيئة ---
GCP_KEY_JSON = os.getenv("GCP_KEY_JSON")
if GCP_KEY_JSON:
    try:
        with open("gcp.json", "w", encoding="utf-8") as f:
            f.write(GCP_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
        logger.info("GCP credentials written to gcp.json")
    except Exception as e:
        logger.error("Failed to write gcp.json: %s", e)

PORT = int(os.getenv("PORT", 5000))
BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

# --- قواعد البيانات ---
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

# --- خدمات خارجية ---
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException
from openai import OpenAI
from google.cloud import speech_v1p1beta1 as speech

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None

# --- FastAPI ---
app = FastAPI()
os.makedirs("public/tts", exist_ok=True)
app.mount("/public", StaticFiles(directory="public"), name="public")

# حالة المكالمات البسيطة في الذاكرة
CALL_STATE: dict[str, dict] = {}

# أداة حفظ السجل

def log_conv(call_sid: str, turn: int, user_text: str, intent: str, tool_called: str, tool_result: str, reply_text: str, reply_audio_url: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO conversations (timestamp, call_sid, turn, user_text, intent, tool_called, tool_result, reply_text, reply_audio_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.datetime.utcnow().isoformat(), call_sid, turn, user_text, intent or "", tool_called or "", tool_result or "", reply_text or "", reply_audio_url or ""),
    )
    conn.commit()
    conn.close()

# مولّد متزامن صالح لـ gRPC (يُغذَّى من الـWS)
from google.cloud.speech_v1p1beta1 import StreamingRecognizeRequest
class SpeechRequestIterator:
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

# 1) Twilio يبدأ المكالمة
@app.post("/voice")
async def voice(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")

    state = CALL_STATE.get(call_sid)
    if state is None:
        CALL_STATE[call_sid] = {"turn": 0}
        first_turn = True
    else:
        first_turn = state.get("turn", 0) == 0

    # عنوان WebSocket
    from urllib.parse import urlparse
    ws_host = urlparse(BASE_URL).netloc or request.url.netloc
    wss_url = f"wss://{ws_host}/media?callSid={call_sid}"

    # تحية أول مرة فقط
    say_block = """
  <Say language=\"ar-SA\" voice=\"Polly.Zeina\">مرحبًا بكم في سمارت كول سنتر. تفضّل بالحديث، أنا أُصغي إليك.</Say>
    """ if first_turn else ""

    twiml = f"""
<Response>
  <Start>
    <Stream url=\"{wss_url}\"/>
  </Start>{say_block}
  <Pause length=\"60\"/>
</Response>
""".strip()
    logger.info("/voice: started call_sid=%s -> stream %s (first_turn=%s)", call_sid, wss_url, first_turn)
    return Response(content=twiml, media_type="text/xml; charset=utf-8")

# 2) WebSocket لاستقبال الصوت من Twilio Media Streams
@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    query = ws.scope.get("query_string", b"").decode()
    call_sid = ""
    if "callSid=" in query:
        call_sid = query.split("callSid=")[1].split("&")[0]
    logger.info("WS connected: call_sid=%s", call_sid)

    # تهيئة Google STT (Streaming)
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

    # مُولّد متزامن صالح لـgRPC + بدء الاستهلاك
    req_iter = SpeechRequestIterator()
    stt_responses = speech_client.streaming_recognize(streaming_config, req_iter)
    stt_task = asyncio.create_task(_consume_stt_responses(stt_responses, call_sid))

    try:
        while True:
            msg = await ws.receive_text()
            event = json.loads(msg)
            etype = event.get("event")
            if etype == "media":
                b64 = event.get("media", {}).get("payload")
                if b64:
                    ulaw = base64.b64decode(b64)
                    pcm = audioop.ulaw2lin(ulaw, 2)  # 16-bit PCM
                    req_iter.push(pcm)
            elif etype == "stop":
                logger.info("WS stop [%s]", call_sid)
                break
    except WebSocketDisconnect:
        logger.info("WS disconnect [%s]", call_sid)
    except Exception as e:
        logger.exception("WS error [%s]: %s", call_sid, e)
    finally:
        req_iter.close()
        try:
            await stt_task
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
        logger.info("connection closed")

# لف iterator متزامن إلى async
async def _aiter(sync_iterable):
    loop = asyncio.get_event_loop()
    iterator = iter(sync_iterable)
    while True:
        try:
            item = await loop.run_in_executor(None, next, iterator)
        except StopIteration:
            break
        yield item

# استهلاك نتائج STT والتعامل مع الجُمل النهائية
async def _consume_stt_responses(stt_responses, call_sid: str):
    async for resp in _aiter(stt_responses):
        for result in resp.results:
            transcript = result.alternatives[0].transcript.strip()
            if result.is_final and transcript:
                logger.info("STT FINAL* [%s]: %s", call_sid, transcript)
                await _handle_user_turn(call_sid, transcript)

# LLM + أدوات مبسطة
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
            answer = msg.content.strip()
    except Exception as e:
        logger.error("LLM error: %s", e)
        answer = "عذراً، لم أفهم جيداً. هل يمكنك التوضيح؟"

    return intent, tool_called, tool_result, answer

# أدوات وهمية
def _mock_lookup_balance(customer_id: str):
    return {"customer_id": customer_id or "12345", "balance": 150.50}

def _mock_open_ticket(summary: str):
    return f"T-{str(uuid.uuid4())[:8]}"

# تشكيل عربي بسيط عبر GPT
async def _arabic_diacritize_and_style(text: str) -> str:
    if not openai_client:
        return text
    prompt = (
        "أضف التشكيل العربي للنص التالي بدقة وتهذيب، وأدرج إشارات توقف مناسبة مثل [pause=300ms] دون مبالغة.

" f"النص: {text}"
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

# TTS متوافق مع مكتبة OpenAI (إزالة الوسيط format)
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
        logger.info("TTS OK -> %s", url)
        return url
    except TypeError:
        # fallback لإصدارات لا تدعم response_format
        with openai_client.audio.speech.with_streaming_response.create(
            model="gpt-4o-mini-tts",
            voice="alloy",
            input=text,
        ) as resp:
            resp.stream_to_file(out_path)
        logger.info("TTS OK (fallback) -> %s", url)
        return url
    except Exception as e:
        logger.error("TTS error: %s", e)
        return None

# معالجة دورة حوار واحدة
async def _handle_user_turn(call_sid: str, user_text: str):
    intent, tool_called, tool_result, reply_text = await _llm_plan_and_reply(user_text)
    prepared_text = await _arabic_diacritize_and_style(reply_text)
    mp3_url = await _synthesize_tts(prepared_text)

    if twilio_client:
        try:
            if mp3_url:
                twiml = f"""
<Response>
  <Play>{mp3_url}</Play>
  <Redirect method=\"POST\">/voice</Redirect>
</Response>
""".strip()
            else:
                safe_text = prepared_text or reply_text or "حسنًا، هل يمكنك التوضيح أكثر؟"
                twiml = f"""
<Response>
  <Say language=\"ar-SA\" voice=\"Polly.Zeina\">{safe_text}</Say>
  <Redirect method=\"POST\">/voice</Redirect>
</Response>
""".strip()
            twilio_client.calls(call_sid).update(twiml=twiml)
            logger.info("CALL UPDATE [%s]: played mp3=%s", call_sid, bool(mp3_url))
        except TwilioException as e:
            logger.error("Twilio update error [%s]: %s", call_sid, e)

    state = CALL_STATE.get(call_sid, {"turn": 0})
    state["turn"] = state.get("turn", 0) + 1
    CALL_STATE[call_sid] = state
    log_conv(call_sid, state["turn"], user_text, intent, tool_called, tool_result, reply_text, mp3_url or "")

@app.get("/health")
async def health():
    return PlainTextResponse("OK")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
