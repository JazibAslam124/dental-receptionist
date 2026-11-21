import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
ROOT = Path(__file__).resolve().parent.parent

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")
# gpt-oss models always reason; "low" keeps replies fast and saves free-tier tokens (low | medium | high)
GROQ_REASONING_EFFORT = os.getenv("GROQ_REASONING_EFFORT", "low")

DB_PATH = os.getenv("DB_PATH", str(ROOT / "receptionist.db"))
CLINIC_FILE = os.getenv("CLINIC_FILE", str(ROOT / "data" / "clinic.json"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")
TWILIO_SMS_FROM = os.getenv("TWILIO_SMS_FROM", "")
TWILIO_VOICE = os.getenv("TWILIO_VOICE", "Polly.Joanna")
CLINIC_LANG = os.getenv("CLINIC_LANG", "en-US")
HUMAN_TRANSFER_PHONE = os.getenv("HUMAN_TRANSFER_PHONE", "")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", "") or SMTP_USER

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
STAFF_EMAIL = os.getenv("STAFF_EMAIL", "")