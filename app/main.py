"""FastAPI server: web chat API, WhatsApp webhook, phone-call (voice) webhooks, admin API."""
import base64
import hashlib
import hmac
import re
from contextlib import asynccontextmanager
from xml.sax.saxutils import escape

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import brain, config, db, knowledge


@asynccontextmanager
async def lifespan(_):
    db.init_db()
    yield


app = FastAPI(title="Dental AI Receptionist", lifespan=lifespan)


# ------------------------------------------------------------------ web chat + browser voice demo
class ChatIn(BaseModel):
    session_id: str
    message: str
    channel: str = "web"          # "web" or "voice" (browser voice demo)
    phone: str | None = None


def _clean_sid(s):
    return re.sub(r"[^A-Za-z0-9:_+\-]", "", s)[:80] or "anon"


@app.post("/api/chat")
def api_chat(body: ChatIn):
    channel = body.channel if body.channel in ("web", "voice") else "web"
    return brain.chat("web:" + _clean_sid(body.session_id), channel, body.message, body.phone)


@app.get("/api/history/{session_id}")
def api_history(session_id: str):
    return {"messages": db.load_history("web:" + _clean_sid(session_id), limit=50)}


@app.get("/api/clinic")
def api_clinic():
    c = knowledge.clinic()
    return {"name": c["name"], "assistant": c["assistant_name"], "phone": c["phone"], "address": c["address"]}


# ------------------------------------------------------------------ Twilio helpers
def _twiml(inner):
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>', media_type="application/xml")


async def _twilio_form(request: Request):
    """Parse the form and (when PUBLIC_BASE_URL + auth token are set) verify Twilio's signature."""
    form = await request.form()
    if config.PUBLIC_BASE_URL and config.TWILIO_AUTH_TOKEN:
        url = config.PUBLIC_BASE_URL + request.url.path + (("?" + request.url.query) if request.url.query else "")
        data = url + "".join(k + str(form[k]) for k in sorted(form.keys()))
        expected = base64.b64encode(hmac.new(config.TWILIO_AUTH_TOKEN.encode(), data.encode(), hashlib.sha1).digest()).decode()
        if not hmac.compare_digest(expected, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(403, "Invalid Twilio signature")
    return form


# ------------------------------------------------------------------ WhatsApp (Twilio sandbox, free)
@app.post("/whatsapp")
async def whatsapp(request: Request):
    form = await _twilio_form(request)
    sender, text = form.get("From", ""), (form.get("Body") or "").strip()
    if not text:
        reply = "Hi! I can read text messages only for now. How can I help you?"
    else:
        r = await run_in_threadpool(brain.chat, "wa:" + sender, "whatsapp", text, sender)
        reply = r["reply"]
    return _twiml(f"<Message>{escape(reply)}</Message>")


# ------------------------------------------------------------------ Phone calls (Twilio voice)
BYE = re.compile(r"\b(bye|goodbye|that'?s all|that is all|nothing else|no,? thank(s| you))\b", re.I)


def _say(text):
    return f'<Say voice="{config.TWILIO_VOICE}" language="{config.CLINIC_LANG}">{escape(text)}</Say>'


def _listen(prompt):
    return (f'<Gather input="speech" action="/voice/respond" method="POST" speechTimeout="auto" '
            f'language="{config.CLINIC_LANG}">{_say(prompt)}</Gather>'
            f'{_say("I did not hear anything. Please call again whenever you like. Goodbye.")}<Hangup/>')


@app.post("/voice/incoming")
async def voice_incoming(request: Request):
    form = await _twilio_form(request)
    c = knowledge.clinic()
    greeting = (f"Thank you for calling {c['name']}. I'm {c['assistant_name']}, the virtual receptionist. "
                f"How can I help you today?")
    db.save_message("call:" + form.get("CallSid", "?"), "voice", "assistant", greeting)
    return _twiml(_listen(greeting))


@app.post("/voice/respond")
async def voice_respond(request: Request):
    form = await _twilio_form(request)
    sid, caller, heard = "call:" + form.get("CallSid", "?"), form.get("From", ""), (form.get("SpeechResult") or "").strip()
    if not heard:
        return _twiml(_listen("Sorry, I didn't catch that. Could you say it again?"))
    r = await run_in_threadpool(brain.chat, sid, "voice", heard, caller)
    if r["handoff"]:
        if config.HUMAN_TRANSFER_PHONE:
            return _twiml(_say(r["reply"] + " Please hold while I connect you.") +
                          f"<Dial>{escape(config.HUMAN_TRANSFER_PHONE)}</Dial>")
        return _twiml(_say(r["reply"] + " Our team will call you back shortly. Goodbye.") + "<Hangup/>")
    if BYE.search(heard):
        return _twiml(_say(r["reply"]) + "<Hangup/>")
    return _twiml(_listen(r["reply"]))


# ------------------------------------------------------------------ Admin
@app.get("/api/admin/overview")
def admin_overview(request: Request, token: str = ""):
    supplied = token or request.headers.get("X-Admin-Token", "")
    if not config.ADMIN_TOKEN or not hmac.compare_digest(supplied, config.ADMIN_TOKEN):
        raise HTTPException(403, "Forbidden")
    return db.overview()


app.mount("/", StaticFiles(directory=str(config.ROOT / "static"), html=True), name="static")