# ابحث عن دالة media_stream في app.py
# واستبدل الجزء الخاص بـ Google STT (من السطر 338 تقريباً)
# بهذا الكود الكامل:

@app.websocket("/media")
async def media_stream(ws: WebSocket):
    """WebSocket محسّن مع محادثة قابلة للمقاطعة"""
    global GOOGLE_STT_AVAILABLE
    
    await ws.accept()
    
    # استخراج call_sid
    query = ws.scope.get("query_string", b"").decode()
    call_sid = ""
    if "callSid=" in query:
        try:
            call_sid = query.split("callSid=")[1].split("&")[0]
        except Exception:
            call_sid = ""
    
    logger.info(f"🔌 WebSocket connected: call_sid={call_sid}")
    
    # تهيئة المتغيرات
    req_iter = None
    stt_task = None
    speech_client = None
    test_task = None
    stt_available = GOOGLE_STT_AVAILABLE
    
    # Google STT مع إعدادات محسّنة للمقاطعة
    if stt_available and not TEST_MODE:
        try:
            speech_client = speech.SpeechClient()
            
            streaming_config = speech.StreamingRecognitionConfig(
                config=speech.RecognitionConfig(
                    encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                    sample_rate_hertz=8000,
                    language_code="ar-SA",
                    enable_automatic_punctuation=True,
                    enable_word_time_offsets=False,
                    profanity_filter=False,
                    # إعدادات للمقاطعة السريعة
                    enable_speaker_diarization=False,
                    speech_contexts=[
                        speech.SpeechContext(
                            phrases=[
                                "نعم", "لا", "توقف", "انتظر",
                                "السلام عليكم", "وعليكم السلام",
                                "رصيدي", "رصيد", "الباقة", "باقتي",
                                "فاتورة", "مشكلة", "إنترنت", "شكرا"
                            ],
                            boost=20.0
                        )
                    ]
                ),
                interim_results=True,  # مهم للمقاطعة
                single_utterance=False,
            )
            
            req_iter = SpeechRequestIterator()
            
            # إرسال صوت صامت لتجنب timeout
            logger.info("Initializing STT with silent audio...")
            silence_samples = 800  # 100ms @ 8kHz
            silence_audio = b'\x00\x00' * silence_samples
            for _ in range(5):
                req_iter.push(silence_audio)
            
            stt_responses = speech_client.streaming_recognize(streaming_config, req_iter)
            stt_task = asyncio.create_task(_consume_stt_with_interruption(stt_responses, lambda: call_sid))
            logger.info("✅ Google STT initialized with interruption support")
            
        except Exception as e:
            logger.error(f"Failed to initialize STT: {e}")
            stt_available = False
    
    # وضع الاختبار
    if not stt_available or TEST_MODE:
        logger.warning("⚠️ Running in TEST MODE or STT unavailable")
        test_task = None
    
    try:
        while True:
            msg = await ws.receive_text()
            try:
                event = json.loads(msg)
            except json.JSONDecodeError:
                logger.warning(f"Invalid JSON received")
                continue
                
            etype = event.get("event")
            
            if etype == "start":
                start = event.get("start", {})
                cp = start.get("customParameters")
                
                if cp:
                    if isinstance(cp, dict):
                        if not call_sid:
                            call_sid = cp.get("callSid", "") or start.get("callSid", "")
                    elif isinstance(cp, str):
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
                
                # بدء المحاكاة في TEST_MODE
                if TEST_MODE and call_sid and not test_task:
                    logger.info(f"🧪 Starting TEST simulation for {call_sid}")
                    test_task = asyncio.create_task(_simulate_user_input(call_sid, delay=3))
                    
            elif etype == "media":
                media_payload = event.get("media", {})
                b64 = media_payload.get("payload")
                
                if b64 and req_iter:
                    try:
                        ulaw = base64.b64decode(b64)
                        pcm = audioop.ulaw2lin(ulaw, 2)
                        req_iter.push(pcm)
                    except Exception as e:
                        logger.warning(f"Error processing audio: {e}")
                        
            elif etype == "stop":
                logger.info(f"⏹️ Stream stopped: call_sid={call_sid}")
                break
                
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
            except Exception:
                pass
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


# دالة محسّنة للتعرف على الكلام مع دعم المقاطعة
async def _consume_stt_with_interruption(stt_responses, get_call_sid):
    """معالجة STT مع دعم المقاطعة الفورية"""
    try:
        async for resp in _aiter(stt_responses):
            for result in resp.results:
                # معالجة النتائج المؤقتة للمقاطعة السريعة
                if not result.is_final:
                    interim_text = result.alternatives[0].transcript.strip()
                    if interim_text and len(interim_text) > 2:
                        # كشف محاولة المقاطعة
                        logger.debug(f"🎤 Interim: {interim_text}")
                        
                        # إذا كان المستخدم يحاول المقاطعة
                        interrupt_words = ["انتظر", "توقف", "لحظة", "نعم", "لا", "صح", "خطأ"]
                        if any(word in interim_text for word in interrupt_words):
                            call_sid = get_call_sid()
                            logger.info(f"🔴 Interruption detected: {interim_text}")
                            # يمكن إيقاف الرد الحالي هنا
                            await _handle_interruption(call_sid, interim_text)
                else:
                    # النتيجة النهائية
                    transcript = result.alternatives[0].transcript.strip()
                    if transcript:
                        call_sid = get_call_sid()
                        logger.info(f"🎤 STT Final [{call_sid}]: {transcript}")
                        await _handle_user_turn(call_sid, transcript)
                        
    except Exception as e:
        logger.error(f"STT processing error: {e}")


# دالة جديدة للتعامل مع المقاطعة
async def _handle_interruption(call_sid: str, text: str):
    """معالجة المقاطعة الفورية"""
    if twilio_client and call_sid:
        try:
            # إيقاف التشغيل الحالي فوراً
            twiml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="ar-SA" voice="Polly.Zeina">نعم، أسمعك</Say>
    <Redirect method="POST">/voice</Redirect>
</Response>"""
            
            twilio_client.calls(call_sid).update(twiml=twiml)
            logger.info(f"✅ Interrupted playback for [{call_sid}]")
        except Exception as e:
            logger.error(f"Failed to interrupt: {e}")


# تحسين دالة voice_handler للرسائل الديناميكية
@app.post("/voice")
async def voice_handler(request: Request):
    """معالج Twilio مع رسائل ترحيب ديناميكية"""
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
    
    # رسالة ترحيب ديناميكية
    say_block = ""
    if first_turn:
        # توليد رسالة ترحيب مختلفة كل مرة
        greeting = await _generate_dynamic_greeting()
        say_block = f"""
  <Say language="ar-SA" voice="Polly.Zeina">{greeting}</Say>"""
    
    # TwiML مع دعم المقاطعة
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


# دالة جديدة لتوليد رسائل ترحيب ديناميكية
async def _generate_dynamic_greeting() -> str:
    """توليد رسالة ترحيب مختلفة كل مرة باستخدام AI"""
    if not openai_client:
        # رسائل افتراضية متنوعة
        greetings = [
            "أهلاً وسهلاً بكم في مركز الاتصال الذكي. كيف يمكنني خدمتكم اليوم؟",
            "مرحباً بك! أنا مساعدك الذكي. بماذا أستطيع مساعدتك؟",
            "السلام عليكم ورحمة الله. تفضل، كيف أقدر أساعدك؟",
            "أهلاً بك في خدمة العملاء. أنا هنا لمساعدتك في أي استفسار.",
            "مساء الخير وأهلاً بك. كيف يمكنني أن أكون في خدمتك؟"
        ]
        import random
        return random.choice(greetings)
    
    try:
        # استخدام GPT لتوليد رسالة ترحيب فريدة
        current_time = datetime.datetime.now()
        time_of_day = "صباح" if current_time.hour < 12 else "مساء" if current_time.hour < 18 else "مساء"
        
        prompt = f"""أنت موظف خدمة عملاء ودود في شركة اتصالات.
اكتب رسالة ترحيب قصيرة (10-15 كلمة) للمتصل.
الوقت الآن: {time_of_day}
كن ودوداً ومحترفاً. غيّر الصيغة كل مرة.
لا تكرر نفس الأسلوب. كن مبدعاً.
الرسالة:"""
        
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,  # عالية للتنوع
            max_tokens=50
        )
        
        greeting = response.choices[0].message.content.strip()
        logger.info(f"🎨 Dynamic greeting: {greeting}")
        return greeting
        
    except Exception as e:
        logger.error(f"Failed to generate dynamic greeting: {e}")
        return "مرحباً بكم في مركز الاتصال الذكي. كيف يمكنني مساعدتكم؟"
