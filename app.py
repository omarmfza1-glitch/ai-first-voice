import os, io, base64, uuid, sqlite3, datetime, json, asyncio, re, logging
try:
    import audioop  # Python <= 3.12
except ModuleNotFoundError:
    import audioop_lts as audioop  # بديل متوافق
from typing import Optional
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(levelname)s:smart-cc:%(message)s")
log = logging.getLogger("smart-cc")

load_dotenv()

# --- بيئة ---
GCP_KEY_JSON = os.getenv("GCP_KEY_JSON")
if GCP_KEY_JSON:
    try:
        with open("gcp.json", "w", encoding="utf-8") as f:
            f.write(GCP_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
        log.info("GCP credentials written to gcp.json")
    except Exception as e:
        log.exception(f"GCP credentials write failed: {e}")

PORT = int(os.getenv("PORT", 5000))
BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

# --- قاعدة بيانات ---
DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")
os.makedirs("public/tts", exist_ok=True)
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
conn.commit(); conn.close()

# --- عملاء خارجيين ---
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException
from openai import OpenAI
from google.cloud import speech_v1p1beta1 as speech

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None

# --- FastAPI ---
app = FastAPI()
app.mount("/public", StaticFiles(directory="public"), name="public")

# حالة المكالمة بالذاكرة
CALL_STATE: dict[str, dict] = {}  # {"turn": int, "greeted": bool}

def log_conv(call_sid: str, turn: int, user_text: str, intent: str, tool_called: str,
             tool_result: str, reply_text: str, reply_audio_url: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO conversations (timestamp, call_sid, turn, user_text, intent, tool_called, tool_result, reply_text, reply_audio_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.datetime.utcnow().isoformat(), call_sid, turn, user_text,
         intent or "", tool_called or "", tool_result or "", reply_text or "", reply_audio_url or "")
    )
    conn.commit()
    conn.close()

# Twilio يبدأ الجلسة
@app.post("/voice")
async def voice(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")

    state = CALL_STATE.get(call_sid)
    if not state:
        state = {"turn": 0, "greeted": False}
        CALL_STATE[call_sid] = state
    first_turn = (state.get("turn", 0) == 0) and (not state.get("greeted", False))

    from urllib.parse import urlparse
    ws_host = urlparse(BASE_URL).netloc or request.url.hostname
    wss_url = f"wss://{ws_host}/media?callSid={call_sid}"

    say = ""
    if first_turn:
        say = '<Say language="ar-SA" voice="Polly.Zeina">مرحبًا بكم في سمارت كول سنتر. تفضّل بالحديث، أنا أُصغي إليك.</Say>'
        state["greeted"] = True

    twiml = f"""
<Response>
  <Start><Stream url="{wss_url}"/></Start>
  {say}
  <Pause length="600"/>
</Response>
""".strip()
    log.info(f"/voice: started call_sid={call_sid} -> stream {wss_url} (first_turn={first_turn})")
    return Response(content=twiml, media_type="text/xml; charset=utf-8")

# WebSocket لاستلام الصوت
@app.websocket("/media")
async def media(ws: WebSocket):
    await ws.accept()
    query = ws.scope.get("query_string", b"").decode()
    call_sid = query.split("callSid=")[1].split("&")[0] if "callSid=" in query else ""
    log.info(f"WS connected: call_sid={call_sid}")

    # Google STT
    speech_client = speech.SpeechClient()
    streaming_config = speech.StreamingRecognitionConfig(
        config=speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="ar-SA",
            # alternative_language_codes=["en-US"],  # فعّلها فقط إذا تأكدت أنها مدعومة في حسابك/النموذج
            enable_automatic_punctuation=True,
            use_enhanced=True,
            model="phone_call",
        ),
        interim_results=True,
        single_utterance=False,
    )

    requests_gen = _stt_request_generator()
    stt_responses = speech_client.streaming_recognize(streaming_config, requests_gen)
    stt_task = asyncio.create_task(_consume_stt_responses(stt_responses, call_sid))

    try:
        while True:
            msg = await ws.receive_text()
            event = json.loads(msg)
            et = event.get("event")
            if et == "start":
                log.info("WS start []"); continue
            elif et == "media":
                b64 = event.get("media", {}).get("payload")
                if b64:
                    ulaw = base64.b64decode(b64)
                    pcm = audioop.ulaw2lin(ulaw, 2)
                    await requests_gen.asend(pcm)
            elif et == "stop":
                log.info(f"WS stop [{call_sid}]"); break
    except WebSocketDisconnect:
        log.info("connection closed")
    except Exception as e:
        log.exception(f"WS error: {e}")
    finally:
        try: await requests_gen.aclose()
        except Exception: pass
        await stt_task
        await ws.close()

async def _stt_request_generator():
    from google.cloud.speech_v1p1beta1 import StreamingRecognizeRequest
    try:
        chunk = yield
        while True:
            yield StreamingRecognizeRequest(audio_content=chunk)
            chunk = yield
    except GeneratorExit:
        return

async def _consume_stt_responses(stt_responses, call_sid: str):
    async for resp in _aiter(stt_responses):
        for result in resp.results:
            transcript = result.alternatives[0].transcript.strip()
            if result.is_final and transcript:
                log.info(f"STT FINAL* [{call_sid}]: {transcript}")
                await _handle_user_turn(call_sid, transcript)

async def _aiter(stream):
    loop = asyncio.get_event_loop()
    iterator = iter(stream)
    while True:
        try:
            item = await loop.run_in_executor(None, next, iterator)
        except StopIteration:
            break
        yield item

# دورة حوار واحدة
async def _handle_user_turn(call_sid: str, user_text: str):
    intent, tool_called, tool_result, reply_text = await _llm_plan_and_reply(user_text)
    prepared_text = await _arabic_diacritize_and_style(reply_text)
    safe_tts_text = re.sub(r"\[pause=\d+ms\]", " ", prepared_text)  # لا نمرر العلامات للـTTS

    mp3_url = await _synthesize_tts(safe_tts_text)
    await _twilio_play_and_listen(call_sid, mp3_url, safe_tts_text)

    state = CALL_STATE.get(call_sid, {"turn": 0})
    state["turn"] = state.get("turn", 0) + 1
    CALL_STATE[call_sid] = state
    log_conv(call_sid, state["turn"], user_text, intent, tool_called, tool_result, reply_text, mp3_url)

async def _twilio_play_and_listen(call_sid: str, mp3_url: Optional[str], fallback_text: str):
    if not twilio_client:
        return
    if mp3_url:
        twiml = f"""
<Response>
  <Play>{mp3_url}</Play>
  <Redirect method="POST">/voice</Redirect>
</Response>
""".strip()
    else:
        safe_text = fallback_text or "حسنًا، هل يمكنك التوضيح أكثر؟"
        twiml = f"""
<Response>
  <Say language="ar-SA" voice="Polly.Zeina">{safe_text}</Say>
  <Redirect method="POST">/voice</Redirect>
</Response>
""".strip()

    try:
        await asyncio.sleep(0.15)
        try:
            call = twilio_client.calls(call_sid).fetch()
            if getattr(call, "status", None) not in ("in-progress", "ringing", "queued"):
                return
        except Exception:
            pass
        twilio_client.calls(call_sid).update(twiml=twiml)
        log.info(f"CALL UPDATE [{call_sid}]: played mp3={'True' if mp3_url else 'False'}")
    except TwilioException as e:
        log.exception(f"Twilio update error [{call_sid}]: {e}")

# LLM + أدوات
async def _llm_plan_and_reply(user_text: str):
    intent = None; tool_called=None; tool_result=None; answer = None
    if not openai_client:
        return intent, tool_called, tool_result, "من فضلك أعد طلبك لاحقاً."

    SYSTEM = ("أنت مساعد اتصال لشركة اتصالات سعودية. استنتج النية بإيجاز، واختر أداة واحدة إن لزم، "
              "ثم اكتب جواباً موجزاً مهذباً بالعربية الفصحى. لا تتجاوز جملتين إلى ثلاث.")

    tools = [
        {"type":"function","function":{
            "name":"lookup_balance","description":"جلب رصيد العميل الحالي باستخدام رقم هاتفه أو رقم حسابه.",
            "parameters":{"type":"object","properties":{"customer_id":{"type":"string"}},"required":["customer_id"]}
        }},
        {"type":"function","function":{
            "name":"open_ticket","description":"فتح تذكرة دعم فني مع وصف موجز للمشكلة.",
            "parameters":{"type":"object","properties":{"summary":{"type":"string"}},"required":["summary"]}
        }},
    ]

    try:
        comp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":SYSTEM},{"role":"user","content":user_text}],
            tools=tools, tool_choice="auto", temperature=0.2, max_tokens=200,
        )
        choice = comp.choices[0]
        if getattr(choice, "finish_reason", "") == "tool_calls" and choice.message.tool_calls:
            call = choice.message.tool_calls[0]
            tool_called = call.function.name
            args = json.loads(call.function.arguments or "{}")
            if tool_called == "lookup_balance":
                tool_obj = _mock_lookup_balance(args.get("customer_id",""))
                tool_result = json.dumps(tool_obj, ensure_ascii=False)
                intent = "balance_inquiry"
                answer = f"رصيدك الحالي هو {tool_obj.get('balance','غير متاح')} ريال."
            elif tool_called == "open_ticket":
                ticket_id = _mock_open_ticket(args.get("summary",""))
                intent = "open_ticket"
                answer = f"تم فتح تذكرة رقم {ticket_id}. سنوافيك بالتحديثات."
        if not answer:
            intent = intent or "general_support"
            answer = choice.message.content.strip()
    except Exception as e:
        log.exception(f"LLM error: {e}")
        answer = "عذراً، لم أفهم جيداً. هل يمكنك التوضيح؟"

    return intent, tool_called, tool_result, answer

def _mock_lookup_balance(customer_id: str):
    return {"customer_id": customer_id or "12345", "balance": 150.50}

def _mock_open_ticket(summary: str):
    return f"T-{str(uuid.uuid4())[:8]}"

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
            messages=[{"role":"user","content":prompt}],
            temperature=0.2, max_tokens=250,
        )
        return comp.choices[0].message.content.strip()
    except Exception as e:
        log.exception(f"diacritize error: {e}")
        return text

# TTS مع توافق للإصدارات
async def _synthesize_tts(text: str) -> Optional[str]:
    if not openai_client:
        return None
    try:
        file_id = str(uuid.uuid4())
        out_path = os.path.join("public", "tts", f"{file_id}.mp3")
        url = f"{BASE_URL}/public/tts/{file_id}.mp3"
        try:
            with openai_client.audio.speech.with_streaming_response.create(
                model="gpt-4o-mini-tts", voice="alloy", input=text, response_format="mp3",
            ) as resp:
                resp.stream_to_file(out_path)
            return url
        except TypeError:
            # بعض إصدارات المكتبة لا تدعم response_format
            with openai_client.audio.speech.with_streaming_response.create(
                model="gpt-4o-mini-tts", voice="alloy", input=text,
            ) as resp:
                resp.stream_to_file(out_path)
            return url
    except Exception as e:
        log.exception(f"TTS error: {e}")
        return None

# Status Callback من Twilio (اختياري)
@app.post("/twilio/status")
async def twilio_status(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    log.info(f"Twilio status: {call_sid} -> {call_status}")
    return PlainTextResponse("OK")

# Health
@app.get("/health")
async def health():
    return PlainTextResponse("OK")
