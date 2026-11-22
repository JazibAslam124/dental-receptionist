# AI Dental Receptionist

An AI receptionist for a dental clinic. Patients can talk to it through a **website chat**, **WhatsApp**, or a **phone call**. It answers questions about services and prices, checks the real calendar, books, reschedules and cancels appointments, sends confirmations, and hands the conversation to a human when it can't help.

Both agents (chatbot and voicebot) share **one brain, one knowledge base and one calendar**, so a booking made by phone blocks the same slot in web chat.

Everything runs on free tools: Groq for the language model, SQLite for storage, and Twilio's free sandbox and trial for WhatsApp and phone.

> **Status:** working demo. The web chat is tested end to end with the real model. WhatsApp and phone are built but need your own Twilio account to try. See [Current status](#current-status).

## What it does

| Capability | How it works |
|---|---|
| FAQs, services and pricing | Answers come from `data/clinic.json`. The prompt forbids inventing prices. |
| Real-time availability | The bot must check the live calendar before offering a time. Two dentists, opening hours, lunch break, holidays and a minimum notice period are modelled. |
| Book, reschedule, cancel | Done through tool calls. Booking asks for confirmation first and ignores repeated "yes" messages. |
| Security on changes | Rescheduling or cancelling needs the booking reference **and** the matching phone number. |
| Customer details | Collects name, phone and optional email. On WhatsApp and phone calls, the number is filled in automatically. |
| Confirmations | Sent on book, reschedule and cancel (email, WhatsApp or SMS), and logged in the staff view. |
| Human handoff | Creates a handoff record with an AI-written summary and alerts staff. Phone calls can be forwarded to a real number. If the AI provider is down, it hands off automatically. |
| Conversation history | Stored per web session, WhatsApp number or call. The last 16 messages are sent to the model on each turn. |

## Architecture

```
Website chat ─┐
WhatsApp ─────┼─► brain.py (LLM + tools) ─► clinic.json · calendar (SQLite) · history · confirmations · handoff
Phone calls ──┘
```

The channels are thin adapters. Each one passes a message to the same `brain.py`, then sends back the reply.

**The model decides what to do, but the code controls what happens.** The model can't write to the database directly. It can only call functions that validate their input: check availability, book, reschedule, cancel, and hand off to a human. Each booking is re-checked inside a lock, so two people can't take the same slot.

## Tech stack

- **Server:** Python, FastAPI
- **LLM:** Groq, `openai/gpt-oss-120b`, with `openai/gpt-oss-20b` as an automatic fallback on rate limits
- **Database:** SQLite
- **WhatsApp and voice:** Twilio (free sandbox and trial number). Twilio handles speech-to-text and text-to-speech; Groq does the thinking.
- **Notifications:** Gmail SMTP, Twilio, and a Telegram bot for staff alerts
- **Frontend:** plain HTML and CSS, no build step
- **Tests:** pytest, with a scripted fake LLM so no API key is needed

## Project structure

```
dental-receptionist/
├── .env.example          copy to .env and fill in
├── README.md
├── requirements.txt
├── app/
│   ├── config.py         settings from .env
│   ├── db.py             SQLite tables
│   ├── knowledge.py      loads clinic.json
│   ├── calendar_service.py   availability and booking logic
│   ├── notifier.py       confirmations and staff alerts
│   ├── tools.py          functions the AI is allowed to call
│   ├── brain.py          LLM loop, prompt, history, reply cleanup
│   └── main.py           FastAPI app and webhooks
├── data/
│   └── clinic.json       services, prices, hours, FAQs (sample data)
├── static/
│   ├── index.html        website chat
│   ├── voice.html        browser voice demo
│   ├── admin.html        staff view
│   └── style.css
└── tests/
    └── test_flow.py
```

## Getting started

You need Python 3.10 or newer and a free Groq API key from [console.groq.com/keys](https://console.groq.com/keys).

**1. Clone and install**

Windows (PowerShell):
```powershell
git clone https://github.com/JazibAslam124/dental-receptionist.git
cd dental-receptionist
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

macOS or Linux:
```bash
git clone https://github.com/JazibAslam124/dental-receptionist.git
cd dental-receptionist
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

**2. Add your settings** in `.env`:
```
GROQ_API_KEY=your_key_here
GROQ_MODEL=openai/gpt-oss-120b
GROQ_FALLBACK_MODEL=openai/gpt-oss-20b
GROQ_REASONING_EFFORT=low
ADMIN_TOKEN=pick-a-password
```
Never commit `.env`. It is listed in `.gitignore`.

**3. Run the tests** (no API key needed):
```bash
python -m pytest -q
```

**4. Start the app:**
```bash
python -m uvicorn app.main:app --reload --port 8000
```

Then open:
- `http://localhost:8000` for the chat
- `http://localhost:8000/voice.html` for the voice demo (Chrome or Edge, allow the microphone)
- `http://localhost:8000/admin.html` for the staff view (enter your `ADMIN_TOKEN`)

## Try it

1. "How much is teeth whitening?" The answer comes from `clinic.json`.
2. "I'd like a check-up and cleaning next Monday morning." It checks the calendar and offers real times.
3. Pick a time and give a name and phone number. It repeats the details and asks before booking.
4. Open the staff view. The appointment and the confirmation appear there.
5. "I want to speak to a person." A handoff appears in the staff view.

## Make it yours

Edit `data/clinic.json` to change the clinic name, services, prices, opening hours, holidays, dentists and FAQs. Set your real timezone there too (for example `Europe/Berlin`), otherwise "tomorrow" and opening hours are worked out in UTC. For another type of business, such as a salon or a vet, swap in a different data file. The structure stays the same.

## WhatsApp and phone (optional)

Both need a public HTTPS address that Twilio can reach. For testing, a free Cloudflare tunnel works:

```bash
cloudflared tunnel --url http://localhost:8000
```

Put the printed link into your Twilio WhatsApp sandbox and phone number webhook settings. Twilio trial accounts can only message or call numbers you have verified, and they play a short trial notice at the start of calls. To forward a call to a person, set `HUMAN_TRANSFER_PHONE` in `.env`.

## Current status

| Area | Status |
|---|---|
| FAQs and pricing from clinic data | Tested live |
| Calendar availability | Tested live |
| Collecting details and booking with confirmation | Tested live |
| Repeated "yes" does not double-book | Tested live |
| Human handoff (web) | Tested live |
| Reply cleanup (no markdown artifacts, odd spaces) | Tested live |
| Automated tests | 9 passing |
| Reschedule and cancel with the real model | Logic covered by tests; live test pending |
| Wrong-phone security check | Logic covered by tests; live test pending |
| Browser voice demo | Built, not yet tested |
| WhatsApp and phone via Twilio | Built, not yet tested |
| Email and Telegram delivery | Built, not yet tested |

## Limitations

- The clinic data, prices and timezone in the repo are **sample data**.
- It handles routine requests. It is not a replacement for a whole front desk, and sensitive matters go to a human.
- SQLite is fine for a demo. A real deployment should use Postgres and an always-on server.
- Appointment data is personal data. Before using it with real patients, review GDPR or HIPAA requirements, data processing agreements with your providers, and back up the database.
- The Twilio WhatsApp sandbox is for testing. A production WhatsApp number needs Meta business approval.

## Roadmap

- Appointment reminders to reduce no-shows
- Google Calendar sync
- Voice-note transcription with Groq's Whisper
- More languages
- Postgres and monitoring for production use

## Author

Built by [Jazib Aslam](https://github.com/JazibAslam124).