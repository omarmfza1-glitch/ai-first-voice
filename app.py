import os
import json
import base64
import logging
import queue
import threading
from urllib.parse import urlparse

# μ-law -> PCM16 fallback for Python 3.13
try:
    import audioop  # Python <= 3.12
except ModuleNotFoundError:
    import audioop_lts as audioop  # pip install audioop-lts

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response, PlainTextResponse
from google.cloud import speech_v1p1beta1 as speech
from dotenv import load_dotenv

# -------------------------------------------------
# Boot
# -------------------------------------------------
load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
log = logging.getLogger("smart-cc")

# GCP key (supports JSON-in-env)
GCP_KEY_JSON = os.getenv("GCP_KEY_JSON")
if GCP_KEY_JSON and GCP_KEY_JSON.strip().startswith("{"):
    try:
        with open("gcp.json", "w", encoding="utf-8") as f:
            f.write(GCP_KEY_JSON)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
        log.info("✅ GCP credentials written to gcp.json")
    except Exception as e:
        log.error("Failed to write gcp.json: %s", e)

PORT = int(os.getenv("PORT", 5000))
BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}")

# One shared client (thread-safe according to gRPC client design)
speech_client = speech.SpeechClient()
log.info("✅ Google Cloud Speech Client initialized")

app = FastAPI()

# -------------------------------------------------
# Helpers
# -------------------------------------------------

def _wss_url_for(request: Request, call_sid: str) -> str:
    """Build a WSS URL pointing to this app for Twilio Media Streams."""
    # Prefer BASE_URL host; fall back to incoming host
    host = urlparse(BASE_URL).netloc or request.url.netloc
    return f"wss://{host}/twilio/media?callSid={call_sid}"


def _twiml_response(xml_body: str) -> Response:
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>\n{xml_body}""".strip()
    return Response(content=xml, media_type="text/xml; charset=utf-8")


# -------------------------------------------------
# Twilio HTTP Endpoints
# -------------------------------------------------
@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    """Greets the caller and starts the Media Stream right away.
    IMPORTANT: <Pause> keeps the call open; WITHOUT it Twilio will hang up after Say.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")
    wss_url = _wss_url_for(request, call_sid)
    log.info("📞 Greeting handler: Welcoming call %s and starting stream %s", call_sid, wss_url)

    twiml = f"""
<Response>
  <Start>
    <Stream url="{wss_url}"/>
  </Start>
  <Say language="ar-SA">مرحبًا بك! تفضّل بالحديث، أنا أستمع إليك.</Say>
  <Pause length="60"/>
</Response>
"""
    return _twiml_response(twiml)


@app.post("/twilio/status")
async def twilio_status(request: Request):
    form = await request.form()
    call_sid = form.get("CallSid", "")
    call_status = form.get("CallStatus", "")
    log.info("📞 Call Status: %s -> %s", call_sid, call_status)
    return PlainTextResponse("")


# -------------------------------------------------
# Google STT Streaming (blocking API bridged via thread)
# -------------------------------------------------
class AudioQueue:
    def __init__(self):
        self.q: "queue.Queue[bytes | None]" = queue.Queue()
        self.closed = False

    def push(self, chunk: bytes):
        if not self.closed:
            self.q.put(chunk)

    def close(self):
        if not self.closed:
            self.closed = True
            self.q.put(None)

    def request_iter(self, streaming_config: speech.StreamingRecognitionConfig):
        # First request MUST contain the streaming_config
        yield speech.StreamingRecognizeRequest(streaming_config=streaming_config)
        while True:
            chunk = self.q.get()
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)


def start_stt_thread(call_sid: str, aq: AudioQueue):
    cfg = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=8000,
        language_code="ar-SA",
        enable_automatic_punctuation=True,
        use_enhanced=True,
        model="phone_call",
    )
    streaming_cfg = speech.StreamingRecognitionConfig(
        config=cfg,
        interim_results=True,
        single_utterance=False,
    )

    def _run():
        try:
            responses = speech_client.streaming_recognize(requests=aq.request_iter(streaming_cfg))
            for resp in responses:
                for result in resp.results:
                    alt = result.alternatives[0]
                    if result.is_final:
                        log.info("📝 STT FINAL [%s]: %s", call_sid, alt.transcript.strip())
                    else:
                        # Comment out if too chatty
                        pass
        except Exception as e:
            log.error("❌ STT thread error [%s]: %s", call_sid, e)

    t = threading.Thread(target=_run, name=f"stt-{call_sid}", daemon=True)
    t.start()
    return t


# -------------------------------------------------
# Twilio Media Streams WebSocket
# -------------------------------------------------
@app.websocket("/twilio/media")
async def twilio_media(ws: WebSocket):
    await ws.accept()
    try:
        qs = ws.scope.get("query_string", b"").decode()
        call_sid = ""
        if "callSid=" in qs:
            call_sid = qs.split("callSid=")[1].split("&")[0]
        log.info("▶️ WS Receiver: Stream started for call: %s", call_sid)

        aq = AudioQueue()
        stt_thread = start_stt_thread(call_sid, aq)

        frames = 0
        while True:
            try:
                msg = await ws.receive_text()
            except WebSocketDisconnect:
                break

            try:
                event = json.loads(msg)
            except Exception:
                continue

            etype = event.get("event")
            if etype == "start":
                log.info("🎬 start event for %s", call_sid)
            elif etype == "media":
                payload_b64 = event.get("media", {}).get("payload")
                if not payload_b64:
                    continue
                ulaw = base64.b64decode(payload_b64)
                pcm16 = audioop.ulaw2lin(ulaw, 2)  # bytes -> 16-bit PCM
                aq.push(pcm16)
                frames += 1
                if frames == 1:
                    log.info("🎙️ First media frame received for %s (frames so far: 1)", call_sid)
                elif frames % 100 == 0:
                    log.info("🎙️ Media frames forwarded to STT for %s: %d", call_sid, frames)
            elif etype == "mark":
                pass
            elif etype == "stop":
                log.info("⏹️ WS Receiver: Stream stopped. Ending queue.")
                break

    except Exception as e:
        log.error("WS error: %s", e)
    finally:
        try:
            aq.close()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
        log.info("connection closed")


# -------------------------------------------------
# Health
# -------------------------------------------------
@app.get("/health")
async def health():
    return PlainTextResponse("OK")


# -------------------------------------------------
# Local dev entry
# -------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
