# استبدل قسم Google STT في media_stream (حوالي السطر 340-360)

    if stt_available and not TEST_MODE:
        try:
            speech_client = speech.SpeechClient()
            
            # إعدادات محسّنة لتجنب timeout
            streaming_config = speech.StreamingRecognitionConfig(
                config=speech.RecognitionConfig(
                    encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                    sample_rate_hertz=8000,
                    language_code="ar-SA",
                    # إزالة model لأنه يسبب مشاكل
                    # model="latest_long",
                    enable_automatic_punctuation=True,
                    enable_word_time_offsets=False,
                    profanity_filter=False,
                    # إضافة كلمات مساعدة
                    speech_contexts=[
                        speech.SpeechContext(
                            phrases=[
                                "السلام عليكم",
                                "وعليكم السلام", 
                                "رصيدي",
                                "رصيد",
                                "الباقة",
                                "باقتي",
                                "فاتورة",
                                "فاتورتي",
                                "مشكلة",
                                "إنترنت",
                                "الإنترنت",
                                "بطيء",
                                "شكرا",
                                "شكراً",
                                "مع السلامة"
                            ],
                            boost=20.0  # تعزيز التعرف على هذه الكلمات
                        )
                    ]
                ),
                interim_results=True,
                single_utterance=False,
                # إعدادات مهمة لتجنب timeout
                enable_voice_activity_events=True,
            )
            
            req_iter = SpeechRequestIterator()
            
            # حل مشكلة timeout - إرسال صوت صامت فوراً
            logger.info("Sending initial audio to prevent timeout...")
            # إنشاء صوت صامت (صفر) بصيغة PCM
            silence_duration_ms = 100  # 100ms من الصمت
            silence_samples = int(8000 * silence_duration_ms / 1000)  # 8kHz
            silence_audio = b'\x00\x00' * silence_samples  # 16-bit PCM silence
            
            # إرسال الصمت فوراً لبدء الجلسة
            for _ in range(5):  # إرسال 500ms من الصمت
                req_iter.push(silence_audio)
            
            # بدء التعرف على الكلام
            stt_responses = speech_client.streaming_recognize(streaming_config, req_iter)
            stt_task = asyncio.create_task(_consume_stt_responses_improved(stt_responses, lambda: call_sid))
            logger.info("✅ Google STT initialized successfully for call")
            
        except Exception as e:
            logger.error(f"Failed to initialize STT: {e}")
            stt_available = False
            # تفعيل وضع الاختبار تلقائيًا عند فشل STT
            logger.warning("⚠️ STT failed, you may want to enable TEST_MODE")


# دالة محسّنة لمعالجة نتائج STT
async def _consume_stt_responses_improved(stt_responses, get_call_sid):
    """معالجة محسّنة لنتائج التعرف على الكلام"""
    try:
        consecutive_empty = 0
        async for resp in _aiter(stt_responses):
            for result in resp.results:
                # عرض النتائج المؤقتة للتشخيص
                if not result.is_final:
                    interim_text = result.alternatives[0].transcript.strip()
                    if interim_text:
                        logger.debug(f"🎤 STT Interim: {interim_text}")
                        consecutive_empty = 0
                else:
                    # النتيجة النهائية
                    transcript = result.alternatives[0].transcript.strip()
                    if transcript:
                        call_sid = get_call_sid()
                        confidence = result.alternatives[0].confidence if hasattr(result.alternatives[0], 'confidence') else 0
                        logger.info(f"🎤 STT Final [{call_sid}]: '{transcript}' (confidence: {confidence:.2f})")
                        
                        # معالجة النص فقط إذا كانت الثقة كافية
                        if confidence > 0.5 or not hasattr(result.alternatives[0], 'confidence'):
                            await _handle_user_turn(call_sid, transcript)
                        else:
                            logger.warning(f"Low confidence ({confidence:.2f}), ignoring: {transcript}")
                        consecutive_empty = 0
                    else:
                        consecutive_empty += 1
                        if consecutive_empty > 10:
                            logger.debug("Too many empty results, might be silence")
                            
    except Exception as e:
        if "deadline" in str(e).lower() or "timeout" in str(e).lower():
            logger.warning("STT timeout - this is normal for long silences")
        else:
            logger.error(f"STT processing error: {e}")
        
        # في حالة الفشل، يمكن تفعيل وضع الاختبار
        call_sid = get_call_sid()
        if call_sid and TEST_MODE:
            logger.warning(f"STT failed, falling back to test mode for call {call_sid}")
            await _simulate_user_input(call_sid, delay=3)


# تحسين معالجة البيانات الصوتية في media_stream
            elif etype == "media":
                # معالجة البيانات الصوتية
                media_payload = event.get("media", {})
                b64 = media_payload.get("payload")
                
                if b64 and req_iter:
                    try:
                        ulaw = base64.b64decode(b64)
                        # تحويل من μ-law إلى PCM
                        pcm = audioop.ulaw2lin(ulaw, 2)  # 16-bit PCM
                        
                        # إرسال الصوت إلى Google STT
                        req_iter.push(pcm)
                        
                        # سجل مرة واحدة فقط للتأكد
                        if not hasattr(req_iter, '_first_audio_logged'):
                            logger.debug(f"✅ Audio streaming active - receiving {len(pcm)} bytes per chunk")
                            req_iter._first_audio_logged = True
                            
                    except Exception as e:
                        logger.warning(f"Error processing audio: {e}")
                        
                elif b64 and not req_iter and TEST_MODE:
                    # في وضع الاختبار، تجاهل الصوت
                    pass
                elif b64 and not req_iter:
                    # سجل التحذير مرة واحدة فقط
                    if not hasattr(ws, '_audio_warning_logged'):
                        logger.warning("Audio received but STT not initialized - check Google Cloud setup")
                        ws._audio_warning_logged = True
