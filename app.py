# استبدل دالة voice_handler بهذا الكود في app.py

@app.post("/voice")
async def voice_handler(request: Request):
    """معالج Twilio باستخدام Gather بدلاً من Media Streams"""
    form = await request.form()
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")
    
    # تحديث حالة المكالمة
    if call_sid not in CALL_STATE:
        CALL_STATE[call_sid] = {
            "turn": 0,
            "from_number": from_number,
            "start_time": datetime.datetime.utcnow()
        }
    
    state = CALL_STATE[call_sid]
    turn = state.get("turn", 0)
    
    # رسالة ترحيبية في الدور الأول فقط
    if turn == 0:
        prompt = "مرحبًا بكم في مركز الاتصال الذكي. كيف يمكنني مساعدتك؟"
    else:
        prompt = "هل تريد شيئًا آخر؟"
    
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Gather input="speech" 
            language="ar-SA" 
            action="{BASE_URL}/process-speech" 
            method="POST"
            speechTimeout="3"
            timeout="5"
            hints="رصيد,باقة,فاتورة,مشكلة,إنترنت,شكرا">
        <Say language="ar-SA" voice="Polly.Zeina">{prompt}</Say>
    </Gather>
    <Say language="ar-SA" voice="Polly.Zeina">عذرًا، لم أسمعك. الرجاء المحاولة مرة أخرى.</Say>
    <Redirect>{BASE_URL}/voice</Redirect>
</Response>"""
    
    logger.info(f"📞 Voice handler: call_sid={call_sid}, turn={turn}")
    return Response(content=twiml, media_type="text/xml; charset=utf-8")

@app.post("/process-speech")
async def process_speech(request: Request):
    """معالجة الكلام المُتعرف عليه من Twilio"""
    form = await request.form()
    speech_result = form.get("SpeechResult", "")
    confidence = float(form.get("Confidence", "0.0"))
    call_sid = form.get("CallSid", "")
    from_number = form.get("From", "")
    
    logger.info(f"🎤 Twilio STT: '{speech_result}' (confidence: {confidence:.2f})")
    
    # إذا كانت الثقة منخفضة
    if confidence < 0.5:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="ar-SA" voice="Polly.Zeina">
        عذرًا، لم أفهم جيداً. هل يمكنك إعادة المحاولة؟
    </Say>
    <Redirect>{BASE_URL}/voice</Redirect>
</Response>"""
        return Response(content=twiml, media_type="text/xml")
    
    # معالجة النص بـ LLM
    state = CALL_STATE.get(call_sid, {"from_number": from_number, "turn": 0})
    intent, tool_called, tool_result, reply_text = await _llm_plan_and_reply(speech_result, state)
    
    # تحديث حالة المكالمة
    state["turn"] = state.get("turn", 0) + 1
    CALL_STATE[call_sid] = state
    
    # تسجيل المحادثة
    log_conversation(
        call_sid,
        state["turn"],
        speech_result,
        intent,
        tool_called,
        tool_result,
        reply_text,
        "",  # لا يوجد URL صوتي
        0
    )
    
    # إنهاء المكالمة إذا كان الرد وداعًا
    if any(word in reply_text for word in ["مع السلامة", "وداعًا", "في أمان الله"]):
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="ar-SA" voice="Polly.Zeina">{reply_text}</Say>
    <Hangup/>
</Response>"""
    else:
        # الاستمرار في المحادثة
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say language="ar-SA" voice="Polly.Zeina">{reply_text}</Say>
    <Pause length="1"/>
    <Redirect>{BASE_URL}/voice</Redirect>
</Response>"""
    
    return Response(content=twiml, media_type="text/xml")

# يمكنك حذف دالة media_stream لأننا لن نحتاجها
# أو الاحتفاظ بها للمستقبل
