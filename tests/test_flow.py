import json
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from fastapi.testclient import TestClient  # noqa: E402

from app import brain, calendar_service as cal, config, db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setattr(config, "ADMIN_TOKEN", "tok")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "")
    db.init_db()


def next_weekday(n):  # next date >= 7 days out falling on weekday n (0=Mon)
    d = cal.now_local().date() + timedelta(days=7)
    while d.weekday() != n:
        d += timedelta(days=1)
    return d.isoformat()


def test_availability_hours_and_lunch_break():
    mon, sun = next_weekday(0), next_weekday(6)
    times = cal.get_availability(mon, "checkup_cleaning")["available_times"]
    assert times[0] == "09:00" and "13:00" not in times and "14:00" in times
    assert "12:30" not in times  # 45 min would run into the lunch break
    out = cal.get_availability(sun, "checkup_cleaning")
    assert out["available_times"] == [] and out["next_available"]


def test_two_dentists_then_full():
    mon = next_weekday(0)
    a = cal.book("consultation", mon, "10:00", "Ann Lee", "+1 555 111 2222")
    b = cal.book("consultation", mon, "10:00", "Bob Ray", "+1 555 333 4444")
    assert {a["dentist"], b["dentist"]} == {"Dr. Emma Collins", "Dr. Raj Patel"}
    assert "10:00" not in cal.get_availability(mon, "consultation")["available_times"]
    with pytest.raises(cal.CalendarError):
        cal.book("consultation", mon, "10:00", "Cy Dee", "+1 555 777 8888")


def test_reschedule_cancel_and_verification():
    mon, tue = next_weekday(0), next_weekday(1)
    a = cal.book("filling", mon, "09:00", "Ann Lee", "+15551112222", email="a@x.com")
    with pytest.raises(cal.CalendarError):
        cal.cancel(a["ref"], "+15559999999")          # wrong phone
    moved = cal.reschedule(a["ref"], "5551112222", tue, "15:00")
    assert moved["start"].startswith(tue) and moved["start"].endswith("15:00")
    assert "09:00" in cal.get_availability(mon, "filling")["available_times"]
    assert cal.cancel(a["ref"], "+15551112222")["status"] == "cancelled"
    assert cal.upcoming("+15551112222") == []


def scripted(*steps):
    """Fake Groq: each step is a tool-call list or a final text."""
    it = iter(steps)

    def fake(messages, tool_defs):
        s = next(it)
        if isinstance(s, str):
            return {"content": s}
        return {"content": "", "tool_calls": [{"id": f"c{i}", "type": "function",
                "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(s)]}
    return fake


def test_full_booking_conversation(monkeypatch):
    mon = next_weekday(0)
    monkeypatch.setattr(brain, "llm_call", scripted(
        [("check_availability", {"service_id": "checkup_cleaning", "date": mon})],
        "We have 09:00, 09:30 or 10:00. Which suits you?"))
    r1 = brain.chat("s1", "whatsapp", "Cleaning on Monday?", "whatsapp:+4915112345678")
    assert "09:00" in r1["reply"]
    monkeypatch.setattr(brain, "llm_call", scripted(
        [("book_appointment", {"service_id": "checkup_cleaning", "date": mon, "time": "09:30",
                               "patient_name": "Maria Test", "email": "m@test.com"})],
        "Booked! Confirmation sent."))
    r2 = brain.chat("s1", "whatsapp", "09:30 please, Maria Test, m@test.com", "whatsapp:+4915112345678")
    assert "Booked" in r2["reply"]
    appts = cal.upcoming("+4915112345678")           # phone came from the WhatsApp channel, not the LLM
    assert len(appts) == 1 and appts[0]["channel"] == "whatsapp"
    with db.get_db() as c:
        media = {r["medium"] for r in c.execute("SELECT medium FROM outbox")}
    assert {"email", "whatsapp"} <= media             # confirmation logged for both channels
    assert len(db.load_history("s1")) == 4            # conversation history persisted


def test_handoff_and_llm_failure(monkeypatch):
    monkeypatch.setattr(brain, "llm_call", scripted(
        [("transfer_to_human", {"reason": "billing dispute", "summary": "Wants a refund"})], "Connecting you to a colleague."))
    assert brain.chat("s2", "voice", "I want a refund!", "+1555000111")["handoff"] is True

    def boom(*a, **k):
        raise brain.LLMError("down")
    monkeypatch.setattr(brain, "llm_call", boom)
    r = brain.chat("s3", "web", "hello")
    assert r["handoff"] and "technical trouble" in r["reply"]
    with db.get_db() as c:
        assert c.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0] == 2


def test_http_endpoints(monkeypatch):
    monkeypatch.setattr(brain, "llm_call", scripted("Hello! How can I help?", "Sure, *one* moment.", "Bye now!"))
    with TestClient(__import__("app.main", fromlist=["app"]).app) as c:
        j = c.post("/api/chat", json={"session_id": "abc", "message": "hi"}).json()
        assert j["reply"].startswith("Hello")
        assert c.get("/api/history/abc").json()["messages"][0]["content"] == "hi"
        tw = c.post("/voice/incoming", data={"CallSid": "CA1", "From": "+1555"}).text
        assert "<Gather" in tw and "virtual receptionist" in tw
        tw = c.post("/voice/respond", data={"CallSid": "CA1", "From": "+1555", "SpeechResult": "hello"}).text
        assert "<Gather" in tw and "*" not in tw       # markdown stripped for speech
        tw = c.post("/voice/respond", data={"CallSid": "CA1", "From": "+1555", "SpeechResult": "no thanks, bye"}).text
        assert "<Hangup/>" in tw and "<Gather" not in tw
        assert c.get("/api/admin/overview").status_code == 403
        assert c.get("/api/admin/overview?token=tok").status_code == 200
        assert c.get("/").status_code == 200


def test_groq_call_uses_gpt_oss_and_falls_back(monkeypatch):
    monkeypatch.setattr(config, "GROQ_API_KEY", "k")
    monkeypatch.setattr(brain.time, "sleep", lambda *_: None)
    seen = []

    class R:
        def __init__(self, code, body=None):
            self.status_code, self._b, self.text = code, body, "rate limited"

        def json(self):
            return self._b

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.append(json)
        if len(seen) == 1:
            return R(429)
        return R(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(brain.httpx, "post", fake_post)
    assert brain.llm_call([{"role": "user", "content": "hi"}], [])["content"] == "ok"
    assert seen[0]["model"] == "openai/gpt-oss-120b" and seen[1]["model"] == "openai/gpt-oss-20b"
    assert seen[0]["reasoning_effort"] == "low" and seen[0]["max_completion_tokens"] >= 1000
    assert "max_tokens" not in seen[0]


def test_repeated_booking_is_idempotent(monkeypatch):
    from app import tools
    mon = next_weekday(0)
    ctx = {"session_id": "s", "channel": "web", "phone": "+491511234567"}
    args = {"service_id": "checkup_cleaning", "date": mon, "time": "10:00", "patient_name": "Test Patient"}
    r1 = tools.run_tool("book_appointment", dict(args), ctx)
    r2 = tools.run_tool("book_appointment", dict(args), ctx)
    assert r1["ok"] and r2["ok"] and r2.get("already_booked") and r1["reference"] == r2["reference"]
    with db.get_db() as c:
        assert c.execute("SELECT COUNT(*) FROM appointments").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM outbox WHERE medium='sms'").fetchone()[0] == 1   # one confirmation only


def test_clean_reply_per_channel():
    raw = "Booked for **Monday\u202f23\u202fNov** at\u00a011:00 (ref **BS-1**)."
    assert brain.clean_reply(raw, "web") == "Booked for Monday 23 Nov at 11:00 (ref BS-1)."
    assert brain.clean_reply(raw, "whatsapp") == "Booked for *Monday 23 Nov* at 11:00 (ref *BS-1*)."
    assert "*" not in brain.clean_reply(raw, "voice")

def test_unbacked_claim_is_caught():
    from app import brain
    fake = "I’ve moved your check-up to Tuesday 24 November at 11:00. A new confirmation has been sent to anna@test."
    assert "reschedule_appointment" in brain.unbacked_claims(fake, set())
    assert brain.unbacked_claims(fake, {"reschedule_appointment"}) == []
    assert brain.unbacked_claims("Shall I book this appointment for you?", set()) == []