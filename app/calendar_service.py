"""Real-time availability + booking engine (SQLite is the live calendar).

A slot is free if at least one dentist has no overlapping appointment, the clinic is open,
it is not a holiday and it respects the minimum-notice rule. Writes are serialised with a lock
and re-validated inside the transaction, so two callers can never grab the same slot.
"""
import re
import secrets
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db, knowledge

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_lock = threading.Lock()


class CalendarError(Exception):
    """Business-rule error whose message is safe to show to the AI / customer."""


# ---------- helpers ----------
def now_local():
    return datetime.now(ZoneInfo(knowledge.clinic()["timezone"])).replace(tzinfo=None)


def parse_date(s):
    try:
        return datetime.strptime(str(s).strip(), "%Y-%m-%d").date()
    except ValueError:
        raise CalendarError("Date must be in YYYY-MM-DD format.")


def parse_time(s):
    try:
        return datetime.strptime(str(s).strip(), "%H:%M").time()
    except ValueError:
        raise CalendarError("Time must be in HH:MM 24-hour format.")


def norm_phone(p):
    p = re.sub(r"^whatsapp:", "", str(p or "").strip())
    return re.sub(r"[^\d+]", "", p)


def same_phone(a, b):
    x, y = re.sub(r"\D", "", a or ""), re.sub(r"\D", "", b or "")
    return len(x) >= 6 and len(y) >= 6 and x[-8:] == y[-8:]


def _service(service_id):
    svc = knowledge.get_service(service_id)
    if not svc:
        ids = ", ".join(s["id"] for s in knowledge.clinic()["services"])
        raise CalendarError(f"Unknown service '{service_id}'. Valid service ids: {ids}.")
    return svc


def _windows(day):
    c = knowledge.clinic()
    if day.isoformat() in c.get("holidays", []):
        return []
    return [(datetime.combine(day, parse_time(a)), datetime.combine(day, parse_time(b)))
            for a, b in c["hours"].get(DAYS[day.weekday()], [])]


def _dentists(name=None):
    names = [d["name"] for d in knowledge.clinic()["dentists"]]
    if not name:
        return names
    n = name.lower().replace("dr.", "").replace("dr ", "").strip()
    match = [d for d in names if n and n in d.lower()]
    if not match:
        raise CalendarError(f"No dentist matching '{name}'. Available: {', '.join(names)}.")
    return match


def _busy(conn, dentist, day, exclude_ref=None):
    rows = conn.execute("SELECT ref,start,duration FROM appointments WHERE dentist=? AND status='booked' AND start LIKE ?",
                        (dentist, day.isoformat() + "%")).fetchall()
    out = []
    for r in rows:
        if r["ref"] != exclude_ref:
            s = datetime.fromisoformat(r["start"])
            out.append((s, s + timedelta(minutes=r["duration"])))
    return out


def _slots(conn, day, duration, dentists, exclude_ref=None):
    """Return {start_datetime: [free dentists]} for one day."""
    c = knowledge.clinic()
    step = c.get("slot_minutes", 30)
    earliest = now_local() + timedelta(minutes=c.get("min_notice_minutes", 60))
    busy = {d: _busy(conn, d, day, exclude_ref) for d in dentists}
    result = {}
    for w0, w1 in _windows(day):
        t = w0
        while t + timedelta(minutes=duration) <= w1:
            if t >= earliest:
                end = t + timedelta(minutes=duration)
                free = [d for d in dentists if all(end <= b0 or t >= b1 for b0, b1 in busy[d])]
                if free:
                    result[t] = free
            t += timedelta(minutes=step)
    return result


def _check_day(day):
    c = knowledge.clinic()
    today = now_local().date()
    if day < today:
        raise CalendarError("That date is in the past.")
    if day > today + timedelta(days=c.get("booking_horizon_days", 90)):
        raise CalendarError(f"We only book up to {c.get('booking_horizon_days', 90)} days ahead.")


def _get(conn, ref):
    r = conn.execute("SELECT * FROM appointments WHERE ref=?", (ref,)).fetchone()
    return dict(r) if r else None


def _owned(conn, ref, phone):
    appt = _get(conn, str(ref or "").strip().upper())
    if not appt or not same_phone(appt["phone"], phone):
        # Same message for both cases so references can't be guessed.
        raise CalendarError("I couldn't find a booking matching that reference and phone number.")
    return appt


def _new_ref(conn):
    prefix = knowledge.clinic().get("ref_prefix", "AP")
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        ref = f"{prefix}-" + "".join(secrets.choice(alphabet) for _ in range(5))
        if not _get(conn, ref):
            return ref


def human(appt):
    s = datetime.fromisoformat(appt["start"])
    return s.strftime("%A %d %B %Y at %H:%M")


# ---------- public API ----------
def get_availability(date, service_id, dentist=None):
    svc, day, dentists = _service(service_id), parse_date(date), None
    _check_day(day)
    dentists = _dentists(dentist)
    with db.get_db() as conn:
        slots = _slots(conn, day, svc["duration"], dentists)
        out = {"date": day.isoformat(), "weekday": day.strftime("%A"), "service": svc["name"],
               "duration_minutes": svc["duration"], "available_times": [t.strftime("%H:%M") for t in sorted(slots)]}
        if not slots:
            nxt = []
            for i in range(1, 31):
                d = day + timedelta(days=i)
                if d > now_local().date() + timedelta(days=knowledge.clinic().get("booking_horizon_days", 90)):
                    break
                s = _slots(conn, d, svc["duration"], dentists)
                if s:
                    nxt.append({"date": d.isoformat(), "weekday": d.strftime("%A"),
                                "times": [t.strftime("%H:%M") for t in sorted(s)][:4]})
                if len(nxt) == 3:
                    break
            out["note"] = "Clinic is closed or fully booked on this date."
            out["next_available"] = nxt
    return out


def book(service_id, date, time, name, phone, email=None, dentist=None, notes=None, channel="web"):
    svc, day, t = _service(service_id), parse_date(date), parse_time(time)
    _check_day(day)
    name, phone = (name or "").strip(), norm_phone(phone)
    if len(name) < 2:
        raise CalendarError("A patient name is required.")
    if len(re.sub(r"\D", "", phone)) < 6:
        raise CalendarError("A valid phone number is required.")
    start, dentists = datetime.combine(day, t), _dentists(dentist)
    with _lock, db.get_db() as conn:
        # Same person asking twice for the same booking (e.g. replying "yes" again): return the existing one.
        same_time = conn.execute("SELECT * FROM appointments WHERE start=? AND status='booked'",
                                 (start.isoformat(timespec="minutes"),)).fetchall()
        for r in same_time:
            if same_phone(r["phone"], phone):
                if r["service_id"] != svc["id"]:
                    raise CalendarError("This phone number already has a different appointment at that time.")
                existing = dict(r)
                existing["_existing"] = True
                return existing
        slots = _slots(conn, day, svc["duration"], dentists)
        if start not in slots:
            raise CalendarError("That time is no longer available. Please check availability again.")
        cust = db.upsert_customer(conn, name, phone, email)
        ref, now = _new_ref(conn), db.now_iso()
        conn.execute("""INSERT INTO appointments(ref,customer_id,name,phone,email,service_id,service_name,dentist,start,
                        duration,status,notes,channel,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (ref, cust, name, phone, email, svc["id"], svc["name"], slots[start][0],
                      start.isoformat(timespec="minutes"), svc["duration"], "booked", notes, channel, now, now))
        return _get(conn, ref)


def upcoming(phone):
    now = now_local().isoformat(timespec="minutes")
    with db.get_db() as conn:
        rows = conn.execute("SELECT * FROM appointments WHERE status='booked' AND start>=? ORDER BY start", (now,)).fetchall()
    return [dict(r) for r in rows if same_phone(r["phone"], phone)]


def reschedule(ref, phone, new_date, new_time):
    day, t = parse_date(new_date), parse_time(new_time)
    _check_day(day)
    start = datetime.combine(day, t)
    with _lock, db.get_db() as conn:
        appt = _owned(conn, ref, phone)
        if appt["status"] != "booked":
            raise CalendarError("That booking is already cancelled.")
        order = [appt["dentist"]] + [d for d in _dentists() if d != appt["dentist"]]
        slots = _slots(conn, day, appt["duration"], order, exclude_ref=appt["ref"])
        if start not in slots:
            raise CalendarError("That new time is not available. Please check availability again.")
        free = slots[start]
        chosen = appt["dentist"] if appt["dentist"] in free else free[0]
        conn.execute("UPDATE appointments SET start=?, dentist=?, updated_at=? WHERE ref=?",
                     (start.isoformat(timespec="minutes"), chosen, db.now_iso(), appt["ref"]))
        return _get(conn, appt["ref"])


def cancel(ref, phone):
    with _lock, db.get_db() as conn:
        appt = _owned(conn, ref, phone)
        if appt["status"] != "booked":
            raise CalendarError("That booking is already cancelled.")
        conn.execute("UPDATE appointments SET status='cancelled', updated_at=? WHERE ref=?", (db.now_iso(), appt["ref"]))
        return _get(conn, appt["ref"])