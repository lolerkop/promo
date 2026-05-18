import os

from dotenv import load_dotenv
from telethon.sync import TelegramClient

load_dotenv()

api_id_raw = os.getenv("TELEGRAM_API_ID", "").strip()
api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
session_name = os.getenv("TELEGRAM_SESSION_NAME", "my_account").strip() or "my_account"

if not api_id_raw or not api_hash:
		raise RuntimeError("Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env before generating a session.")

try:
		api_id = int(api_id_raw)
except ValueError as exc:
		raise RuntimeError("TELEGRAM_API_ID must be an integer.") from exc

with TelegramClient(session_name, api_id, api_hash) as client:
		print(f"Session '{session_name}' is ready.")
