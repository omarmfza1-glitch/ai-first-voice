# app.py — مركز اتصال ذكي متكامل (النسخة النهائية v10 - فصل المهام)
# -*- coding: utf-8 -*-

# ============================================================================
# 1. الواردات الأساسية (لم يتغير)
# ============================================================================
import os, base64, uuid, sqlite3, datetime, json, asyncio, logging, re
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
load_dotenv()

# ============================================================================
# 2. إعداد السجلات والخدمات الخارجية (لم يتغير)
# ============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("smart-cc")
from twilio.rest import Client as TwilioClient
from twilio.base.exceptions import TwilioException
from twilio.request_validator import RequestValidator
from openai import OpenAI

# ============================================================================
# 3. متغيرات البيئة والتهيئة العالمية (لم يتغير)
# ============================================================================
PORT = int(os.getenv("PORT", 8000)); BASE_URL = os.getenv("BASE_URL", f"http://127.0.0.1:{PORT}")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY"); TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN"); TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if TWILIO_AUTH_TOKEN else None
speech_async_client, speech_types = None, None
try:
    GCP_KEY_JSON_STR = os.getenv("GCP_KEY_JSON")
    if GCP_KEY_JSON_STR:
        with open("gcp.json", "w", encoding="utf-8") as f: f.write(GCP_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.abspath("gcp.json")
    from google.cloud.speech_v1p1beta1.services import speech
    from google.cloud.speech_v1p1beta1 import types as speech_types_module
    speech_types = speech_types_module; speech_async_client = speech.SpeechAsyncClient()
    logger.info("✅ Google Cloud Speech Async Client initialized successfully.")
except Exception as e: logger.warning(f"⚠️ Google Cloud Speech Async Client could not be initialized: {e}")
if not openai_client: logger.warning("⚠️ OpenAI API key not configured.")
if not twilio_client: logger.warning("⚠️ Twilio client not initialized.")

# ============================================================================
# 4. قاعدة البيانات (لم يتغير)
# ============================================================================
DB_PATH = os.path.join(os.path.dirname(__file__), "db.sqlite3")
def init_database():
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS customers (customer_id TEXT PRIMARY KEY, name TEXT, phone TEXT UNIQUE, current_package TEXT, account_balance REAL, last_bill_date TEXT, status TEXT DEFAULT 'active')")
    cur.execute("CREATE TABLE IF NOT EXISTS conversations (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, call_sid TEXT, turn INTEGER, user_text TEXT, intent TEXT, tool_called TEXT, tool_result TEXT, reply_text TEXT, reply_audio_url TEXT, duration_ms INTEGER)")
    cur.execute("CREATE TABLE IF NOT EXISTS tickets (ticket_id TEXT PRIMARY KEY, customer
