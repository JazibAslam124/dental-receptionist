"""Single source of business knowledge, used by BOTH the chatbot and the voicebot."""
import json
import re
from functools import lru_cache

from . import config

DAY_NAMES = [("mon", "Monday"), ("tue", "Tuesday"), ("wed", "Wednesday"), ("thu", "Thursday"),
             ("fri", "Friday"), ("sat", "Saturday"), ("sun", "Sunday")]


@lru_cache(maxsize=1)
def clinic():
    with open(config.CLINIC_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_service(service_id):
    s = (service_id or "").strip().lower()
    if not s:
        return None
    for svc in clinic()["services"]:
        if svc["id"] == s:
            return svc
    for svc in clinic()["services"]:  # forgiving fallback on the name
        n = svc["name"].lower()
        if s in n or n in s:
            return svc
    return None


def hours_text():
    h = clinic()["hours"]
    lines = []
    for key, label in DAY_NAMES:
        win = h.get(key, [])
        lines.append(f"{label}: " + (", ".join(f"{a}-{b}" for a, b in win) if win else "closed"))
    return "\n".join(lines)


def services_text():
    cur = clinic()
    return "\n".join(f"- id={s['id']} | {s['name']} | {s['duration']} min | price: {s['price']} | {s['description']}"
                     for s in cur["services"])


def relevant_faqs(query, k=4):
    words = {w for w in re.findall(r"[a-z]{3,}", (query or "").lower())}
    scored = []
    for f in clinic()["faqs"]:
        fw = set(re.findall(r"[a-z]{3,}", (f["q"] + " " + f["a"]).lower()))
        score = len(words & fw)
        if score:
            scored.append((score, f))
    scored.sort(key=lambda x: -x[0])
    return [f for _, f in scored[:k]]


def knowledge_block(query=""):
    c = clinic()
    faqs = relevant_faqs(query)
    faq_text = "\n".join(f"Q: {f['q']}\nA: {f['a']}" for f in faqs) or "(no specific FAQ matched)"
    dentists = "\n".join(f"- {d['name']} ({d['specialty']})" for d in c["dentists"])
    return f"""CLINIC: {c['name']}
Address: {c['address']} | Phone: {c['phone']} | Email: {c['email']}
Currency: {c['currency']}
OPENING HOURS (timezone {c['timezone']}):
{hours_text()}
Closed on: {', '.join(c.get('holidays', [])) or 'no listed holidays'}
DENTISTS:
{dentists}
SERVICES AND PRICES (the ONLY source of prices):
{services_text()}
POLICIES:
{chr(10).join('- ' + p for p in c['policies'])}
RELEVANT FAQ:
{faq_text}"""