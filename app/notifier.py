"""Confirmations to customers and alerts to staff. Everything is also written to the `outbox`
table, so you can see exactly what was (or would be) sent even before you configure providers."""
import smtplib
import threading
from email.message import EmailMessage

import httpx

from . import calendar_service as cal, config, db, knowledge


def _email(to, subject, body):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = config.SMTP_FROM, to, subject
    msg.set_content(body)
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as s:
        s.starttls()
        s.login(config.SMTP_USER, config.SMTP_PASS)
        s.send_message(msg)


def _twilio(to, body, whatsapp):
    frm = config.TWILIO_WHATSAPP_FROM if whatsapp else config.TWILIO_SMS_FROM
    to = ("whatsapp:" if whatsapp else "") + to
    r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{config.TWILIO_ACCOUNT_SID}/Messages.json",
                   data={"From": frm, "To": to, "Body": body},
                   auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN), timeout=15)
    r.raise_for_status()


def _telegram(text):
    httpx.post(f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
               json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text}, timeout=15).raise_for_status()


def _dispatch(row_id, fn, *args):
    """Send in a background thread so replies (especially phone calls) are never delayed."""
    def run():
        try:
            fn(*args)
            db.set_outbox_status(row_id, "sent")
        except Exception as e:  # noqa
            db.set_outbox_status(row_id, f"failed: {str(e)[:150]}")
    threading.Thread(target=run, daemon=True).start()


def confirmation_text(appt, kind):
    c = knowledge.clinic()
    when = cal.human(appt)
    if kind == "cancelled":
        return (f"Hi {appt['name']}, your {appt['service_name']} appointment on {when} at {c['name']} "
                f"has been cancelled (ref {appt['ref']}). To rebook, reply here or call {c['phone']}.")
    verb = "confirmed" if kind == "booked" else "rescheduled"
    return (f"Hi {appt['name']}, your appointment at {c['name']} is {verb}:\n"
            f"{appt['service_name']} with {appt['dentist']}\n{when}\nReference: {appt['ref']}\n"
            f"Address: {c['address']}\nTo change or cancel, reply here or call {c['phone']} "
            f"(please give 24h notice).")


def send_confirmation(appt, kind="booked"):
    """Returns the list of channels used, e.g. ['email', 'whatsapp'] or ['logged']."""
    text, used = confirmation_text(appt, kind), []
    subject = f"{knowledge.clinic()['name']} - appointment {kind}"
    if appt.get("email"):
        if config.SMTP_HOST and config.SMTP_USER:
            _dispatch(db.log_outbox("email", appt["email"], subject, text, "queued"), _email, appt["email"], subject, text)
            used.append("email")
        else:
            db.log_outbox("email", appt["email"], subject, text, "logged (SMTP not configured)")
    wa = appt.get("channel") == "whatsapp"
    twilio_ready = config.TWILIO_ACCOUNT_SID and config.TWILIO_AUTH_TOKEN and (
        config.TWILIO_WHATSAPP_FROM if wa else config.TWILIO_SMS_FROM)
    medium = "whatsapp" if wa else "sms"
    if twilio_ready:
        _dispatch(db.log_outbox(medium, appt["phone"], subject, text, "queued"), _twilio, appt["phone"], text, wa)
        used.append(medium)
    else:
        db.log_outbox(medium, appt["phone"], subject, text, "logged (Twilio not configured)")
    return used or ["logged"]


def notify_staff(handoff_id, channel, phone, reason, summary):
    text = (f"[{knowledge.clinic()['name']}] Human needed (#{handoff_id}, via {channel})\n"
            f"Caller/patient: {phone or 'unknown'}\nReason: {reason}\nSummary: {summary}")
    if config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID:
        _dispatch(db.log_outbox("telegram", config.TELEGRAM_CHAT_ID, "handoff", text, "queued"), _telegram, text)
    if config.STAFF_EMAIL and config.SMTP_HOST:
        _dispatch(db.log_outbox("email", config.STAFF_EMAIL, "Human handoff", text, "queued"),
                  _email, config.STAFF_EMAIL, "Human handoff needed", text)
    if not (config.TELEGRAM_BOT_TOKEN or config.STAFF_EMAIL):
        db.log_outbox("staff", "-", "handoff", text, "logged (no staff channel configured)")