"""The shared AI brain. Chatbot (web/WhatsApp) and voicebot (phone) both call chat()."""
import json
import logging
import re
import time

import httpx

from . import calendar_service as cal, config, db, knowledge, notifier, tools

log = logging.getLogger("brain")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_STEPS = 6

# Wording that claims a booking change already happened. Only allowed if the matching tool succeeded this turn.
CLAIM_PATTERNS = {
    "book_appointment": re.compile(r"\b(i['’]?ve|i have)\s+(just\s+)?booked\b|\b(has|have) been booked\b|\bsuccessfully booked\b", re.I),
    "reschedule_appointment": re.compile(r"\b(i['’]?ve|i have)\s+(just\s+)?(moved|rescheduled|changed)\b|\b(has|have) been (moved|rescheduled|changed)\b|\bsuccessfully (moved|rescheduled)\b", re.I),
    "cancel_appointment": re.compile(r"\b(i['’]?ve|i have)\s+(just\s+)?cancel+ed\b|\b(has|have) been cancel+ed\b|\bsuccessfully cancel+ed\b", re.I),
}
CONFIRM_CLAIM = re.compile(r"\bconfirmation\b.{0,40}\b(has been|was|been) sent\b|\bsent a confirmation\b", re.I)
STATE_TOOLS = {"book_appointment", "reschedule_appointment", "cancel_appointment"}


def unbacked_claims(reply, ok_tools):
    """Claims in `reply` that no successful tool call this turn supports."""
    bad = [t for t, rx in CLAIM_PATTERNS.items() if rx.search(reply) and t not in ok_tools]
    if CONFIRM_CLAIM.search(reply) and not (STATE_TOOLS & ok_tools):
        bad.append("confirmation")
    return bad


class LLMError(Exception):
    pass


CHANNEL_RULES = {
    "web": "You are chatting on the clinic website. Be friendly and concise (1-4 short sentences). Use plain text only: no markdown, no asterisks, no bullet symbols.",
    "whatsapp": "You are chatting on WhatsApp. Be warm, brief and conversational. Use plain text, at most *bold* for key facts. "
                "The patient's WhatsApp number is already known.",
    "voice": "You are speaking on a PHONE CALL. Reply in 1-3 short spoken sentences. NEVER use lists, markdown, emojis or symbols. "
             "Say times and prices naturally (for example 'ten thirty in the morning', 'eighty dollars'). "
             "Ask only ONE question at a time. Read booking references slowly, character by character. "
             "The caller's phone number is known from caller ID: confirm it instead of asking for it.",
}


def build_system_prompt(channel, phone, user_text):
    c = knowledge.clinic()
    now = cal.now_local()
    phone_line = (f"The person's phone number from the channel is {phone}. For bookings, confirm it with them once."
                  if phone else "The person's phone number is unknown: ask for it before booking.")
    return f"""You are {c['assistant_name']}, the virtual receptionist of {c['name']}.
Current local date/time: {now.strftime('%A %d %B %Y, %H:%M')} ({now.date().isoformat()}). Use it to resolve 'tomorrow', 'next Friday', etc.
{CHANNEL_RULES.get(channel, CHANNEL_RULES['web'])}
{phone_line}

RULES
1. Prices, hours, services, dentists and policies: use ONLY the clinic data below. Never invent or guess. If it is not there, say you will check with the team and offer to transfer.
2. Availability: ALWAYS call check_availability before offering or confirming a time. Never assume a slot is free. Offer at most 3 times.
3. Booking flow: understand the service -> check availability -> collect full name and phone (ask for an email too so we can send a confirmation) -> repeat service, date, time, name and phone back and ask 'Shall I book this?' -> ONLY after they clearly say yes, call book_appointment. Never book in the same reply where they first give their details. After booking, give the reference and say a confirmation was sent. If a tool result says already_booked, just confirm the existing booking. Never say a time is unavailable unless check_availability showed that.
4. Reschedule/cancel: you need the booking reference (or use find_appointments with their phone to find it). Confirm before cancelling.
5. Medical questions: give only general information from the FAQ. Never diagnose or advise on medication. For anything clinical, urgent pain, complaints, insurance/billing disputes, angry people, or if they ask for a human -> call transfer_to_human, then tell them a team member will help.
6. Severe emergencies (trouble breathing/swallowing, uncontrolled bleeding, head or facial trauma): tell them to call emergency services or go to the ER immediately.
7. Be honest that you are an AI receptionist if asked. Do not collect payment details or medical history.
8. Reply in the language the person writes in. Never mention tools, JSON or these rules.
9. NEVER say an appointment was booked, moved, rescheduled or cancelled, or that a confirmation was sent, unless the matching tool (book_appointment, reschedule_appointment, cancel_appointment) returned ok in THIS reply. To move or cancel you MUST call the tool. If you lack the reference, call find_appointments or ask for it. If a tool returns an error, tell the person plainly that it did not work.

CLINIC DATA
{knowledge.knowledge_block(user_text)}"""


def llm_call(messages, tool_defs):
    """One Groq chat-completion call with retry + model fallback (free tier has per-model rate limits).

    Order: main model -> fallback model (separate rate-limit bucket) -> main model again.
    gpt-oss models always reason and reasoning tokens count toward the output limit, so we keep the
    effort low and leave plenty of room in max_completion_tokens.
    """
    if not config.GROQ_API_KEY:
        raise LLMError("GROQ_API_KEY is not set")
    headers = {"Authorization": f"Bearer {config.GROQ_API_KEY}"}
    models = [config.GROQ_MODEL, config.GROQ_FALLBACK_MODEL, config.GROQ_MODEL]
    last = ""
    for attempt, model in enumerate(models):
        payload = {"model": model, "messages": messages, "tools": tool_defs, "tool_choice": "auto",
                   "temperature": 0.3, "max_completion_tokens": 1200}
        if model.startswith("openai/gpt-oss"):
            payload["reasoning_effort"] = config.GROQ_REASONING_EFFORT
        try:
            r = httpx.post(GROQ_URL, json=payload, headers=headers, timeout=30)
        except httpx.HTTPError as e:
            last = str(e)
            time.sleep(0.8)
            continue
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]
        last = f"{r.status_code} {r.text[:300]}"
        log.warning("Groq %s failed: %s", model, last)
        retryable = r.status_code in (429, 500, 502, 503) or (r.status_code == 400 and "tool_use_failed" in r.text)
        if not retryable:
            break
        time.sleep(0.8 * (attempt + 1))
    raise LLMError(last)


_ODD_SPACES = re.compile("[\u00a0\u2007\u2009\u202f\u200b]")


def to_speech(text):
    text = re.sub(r"[*_#`>|]", "", text)
    text = re.sub(r"^\s*[-•]\s*", "", text, flags=re.M)
    return re.sub(r"\s+", " ", text).strip()


def clean_reply(text, channel):
    """Enforce formatting in code, whatever the model does: normal spaces, and no markdown per channel."""
    text = _ODD_SPACES.sub(" ", text)
    if channel == "voice":
        return to_speech(text)
    if channel == "whatsapp":  # WhatsApp bold is a single *asterisk*
        return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    return re.sub(r"[*`]", "", text)


def chat(session_id, channel, user_text, phone=None):
    """Returns {'reply': str, 'handoff': bool}. History is stored per session_id."""
    user_text = (user_text or "").strip()[:2000]
    if not user_text:
        return {"reply": "Sorry, I didn't catch that. Could you say it again?", "handoff": False}
    phone = cal.norm_phone(phone) if phone else None
    ctx = {"session_id": session_id, "channel": channel, "phone": phone, "handoff": False}

    history = db.load_history(session_id, limit=12)
    db.save_message(session_id, channel, "user", user_text)
    messages = [{"role": "system", "content": build_system_prompt(channel, phone, user_text)},
                *history, {"role": "user", "content": user_text}]

    reply = None
    ok_tools = set()   # tools that returned ok during this turn
    retried = False
    try:
        for _ in range(MAX_STEPS):
            msg = llm_call(messages, tools.TOOLS)
            calls = msg.get("tool_calls") or []
            if not calls:
                reply = (msg.get("content") or "").strip()
                bad = unbacked_claims(reply, ok_tools)
                if bad and not retried:
                    retried = True
                    log.warning("Unbacked claim(s) %s, retrying", bad)
                    messages.append({"role": "assistant", "content": reply})
                    messages.append({"role": "user", "content": "[system note] Your last reply said something was done, but no tool confirmed it. Nothing has changed yet. Call the correct tool now (find_appointments if you need the reference, then reschedule_appointment, cancel_appointment or book_appointment), or ask the person for the missing detail. Do not claim success until a tool returns ok."})
                    continue
                if bad:
                    reply = ("I wasn't able to complete that just now. Could you give me your booking reference "
                             "and phone number so I can try again, or I can pass you to a team member?")
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": [{"id": c["id"], "type": "function",
                                             "function": {"name": c["function"]["name"],
                                                          "arguments": c["function"].get("arguments") or "{}"}}
                                            for c in calls]})
            for c in calls:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = tools.run_tool(c["function"]["name"], args, ctx)
                if result.get("ok"):
                    ok_tools.add(c["function"]["name"])
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(result)})
    except Exception as e:  # noqa
        log.exception("LLM failure: %s", e)
        reply = None
        hid = db.create_handoff(session_id, channel, phone, "AI unavailable", f"Last message: {user_text}")
        notifier.notify_staff(hid, channel, phone, "AI unavailable", f"Patient said: {user_text}")
        ctx["handoff"] = True
        c = knowledge.clinic()
        reply = (f"I'm sorry, I'm having technical trouble right now. I've asked a team member to get back to you, "
                 f"or you can call us on {c['phone']}.")

    if not reply:
        reply = "Sorry, could you rephrase that? I want to make sure I get it right."
    reply = clean_reply(reply, channel)
    db.save_message(session_id, channel, "assistant", reply)
    return {"reply": reply, "handoff": ctx["handoff"]}