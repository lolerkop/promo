import logging
import os
import sqlite3

from aiogram import Dispatcher
from aiogram.client.bot import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("ADMIN_BOT_TOKEN", "")
if not BOT_TOKEN:
    raise ValueError("Не найден 'ADMIN_BOT_TOKEN' в файле .env!")

allowed_ids_str = os.getenv("ADMIN_ALLOWED_IDS", "")
allowed_ids = []
if allowed_ids_str:
    for item in allowed_ids_str.split(","):
        try:
            allowed_ids.append(int(item))
        except ValueError:
            logging.warning(f"Неверный ADMIN_ALLOWED_ID: {item}")

if not allowed_ids:
    logging.warning("Список ADMIN_ALLOWED_IDS пуст или не настроен! Админ-бот не будет доступен.")

storage = MemoryStorage()
dp = Dispatcher(storage=storage)
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
global_reg_cache = {}

TEMP_PHOTO_DIR = "temp_profile_photos"
if not os.path.exists(TEMP_PHOTO_DIR):
    try:
        os.makedirs(TEMP_PHOTO_DIR)
    except Exception as e:
        logging.error(f"Не удалось создать директорию {TEMP_PHOTO_DIR}: {e}")

DEFAULT_AI_BASE_PROMPT = "Ты — копирайтер, который пишет уникальные, интересные и вовлекающие комментарии на разные темы."
DEFAULT_OPENAI_MODEL = "gpt-3.5-turbo"
DEFAULT_G4F_MODEL = "gpt-4o-mini"
DEFAULT_LM_STUDIO_BASE_URL = "http://localhost:1234/v1"
DEFAULT_LM_STUDIO_MODEL = "local-model"
DEFAULT_AI_TEMPERATURE = "0.7"
DEFAULT_AI_MAX_TOKENS = "150"
