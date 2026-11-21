import contextlib
import sqlite3
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT UNIQUE, name TEXT, email TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS appointments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE, customer_id INTEGER,
  name TEXT, phone TEXT, email TEXT, service_id TEXT, service_name TEXT, dentist TEXT,
  start TEXT, duration INTEGER, status TEXT DEFAULT 'booked', notes TEXT, channel TEXT,
  created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS idx_appt_start ON appointments(dentist, start, status);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, channel TEXT, role TEXT, content TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS handoffs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, channel TEXT, phone TEXT,
  reason TEXT, summary TEXT, status TEXT DEFAULT 'open', created_at TEXT);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, medium TEXT, to_addr TEXT, subject TEXT, body TEXT,
  status TEXT, created_at TEXT);
"""


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextlib.contextmanager
def get_db():
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as c:
        c.executescript(SCHEMA)


# ---------- conversation history (shared by chat, WhatsApp and voice) ----------
def save_message(session_id, channel, role, content):
    with get_db() as c:
        c.execute("INSERT INTO messages(session_id,channel,role,content,created_at) VALUES(?,?,?,?,?)",
                  (session_id, channel, role, content, now_iso()))


def load_history(session_id, limit=16):
    with get_db() as c:
        rows = c.execute("SELECT role,content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                         (session_id, limit)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ---------- customers ----------
def upsert_customer(conn, name, phone, email=None):
    row = conn.execute("SELECT id FROM customers WHERE phone=?", (phone,)).fetchone()
    if row:
        conn.execute("UPDATE customers SET name=COALESCE(NULLIF(?,''),name), email=COALESCE(NULLIF(?,''),email) WHERE id=?",
                     (name, email or "", row["id"]))
        return row["id"]
    cur = conn.execute("INSERT INTO customers(phone,name,email,created_at) VALUES(?,?,?,?)",
                       (phone, name, email, now_iso()))
    return cur.lastrowid


# ---------- outbox + handoffs ----------
def log_outbox(medium, to_addr, subject, body, status):
    with get_db() as c:
        return c.execute("INSERT INTO outbox(medium,to_addr,subject,body,status,created_at) VALUES(?,?,?,?,?,?)",
                         (medium, to_addr, subject, body, status, now_iso())).lastrowid


def set_outbox_status(row_id, status):
    with get_db() as c:
        c.execute("UPDATE outbox SET status=? WHERE id=?", (status, row_id))


def create_handoff(session_id, channel, phone, reason, summary):
    with get_db() as c:
        return c.execute("INSERT INTO handoffs(session_id,channel,phone,reason,summary,created_at) VALUES(?,?,?,?,?,?)",
                         (session_id, channel, phone, reason, summary, now_iso())).lastrowid


def overview():
    with get_db() as c:
        q = lambda sql: [dict(r) for r in c.execute(sql).fetchall()]
        return {
            "appointments": q("SELECT ref,name,phone,service_name,dentist,start,status,channel FROM appointments ORDER BY start DESC LIMIT 100"),
            "handoffs": q("SELECT id,channel,phone,reason,summary,status,created_at FROM handoffs ORDER BY id DESC LIMIT 50"),
            "outbox": q("SELECT medium,to_addr,status,body,created_at FROM outbox ORDER BY id DESC LIMIT 50"),
            "conversations": q("SELECT session_id,channel,COUNT(*) AS messages,MAX(created_at) AS last_seen FROM messages GROUP BY session_id ORDER BY MAX(id) DESC LIMIT 50"),
        }