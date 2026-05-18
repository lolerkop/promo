import asyncio
import logging
import os

from aiogram.types import BotCommand

from db import get_config_value, set_config_value, update_all_tables

from .bot_instance import (DEFAULT_AI_BASE_PROMPT, DEFAULT_AI_MAX_TOKENS,
                           DEFAULT_AI_TEMPERATURE, DEFAULT_G4F_MODEL,
                           DEFAULT_OPENAI_MODEL, TEMP_PHOTO_DIR, bot, dp)
from .handlers import (accounts, ai_config, analytics, channels,
                       chat_categories_settings, common, general_settings,
                       groups, profile_update, regular_comment,
                       reporting_settings, responses, task_management)


async def set_bot_commands():
	commands = [
		BotCommand(command="start", description="Главное меню / Перезапуск"),
		BotCommand(command="accounts", description="⚙️ Аккаунты Telethon"),
		BotCommand(command="groups", description="👥 Группы"),
		BotCommand(command="channels", description="📺 Каналы"),
		BotCommand(command="responses", description="💬 Ответы на триггеры"),
		BotCommand(command="ai_settings", description="🤖 Настройки AI"),
		BotCommand(command="analytics", description="📊 Аналитика"),
		BotCommand(command="regular_comment", description="🕰️ Регулярный комментарий"),
		BotCommand(command="reporting", description="⚙️ Настройки Отчетов"),
		BotCommand(command="chat_categories", description="🗂️ Категории чатов"),
		BotCommand(command="general_settings", description="🛠️ Общие настройки"),
		BotCommand(command="tasks", description="📌 Управление задачами")
	]
	await bot.set_my_commands(commands)


async def main_bot_polling_task():
	if not os.path.exists(TEMP_PHOTO_DIR):
		try:
			os.makedirs(TEMP_PHOTO_DIR)
		except Exception as e:
			logging.error(f"Could not create {TEMP_PHOTO_DIR}: {e}")

	chat_categories_dir = "chat_categories"
	if not os.path.exists(chat_categories_dir):
		try:
			os.makedirs(chat_categories_dir)
			logging.info(f"Создана директория для категорий чатов: {chat_categories_dir}")
		except Exception as e:
			logging.error(f"Не удалось создать директорию {chat_categories_dir}: {e}")

	update_all_tables()

	default_configs = {
		"ai_base_prompt": DEFAULT_AI_BASE_PROMPT, "openai_model": DEFAULT_OPENAI_MODEL,
		"g4f_model": DEFAULT_G4F_MODEL, "ai_temperature": DEFAULT_AI_TEMPERATURE,
		"ai_provider": "openai_g4f",
		"ai_max_tokens": DEFAULT_AI_MAX_TOKENS,
		"listen_all": "True",
		"REGULAR_COMMENT_ENABLED": "0", "REGULAR_COMMENT_INTERVAL": "60",
		"reporting_enabled": "False",
		"reporting_target_id": "",
		"ai_base_prompt_conversation": "Ты — дружелюбный и полезный ИИ-собеседник. Продолжай диалог естественно и по существу.",
		"ai_max_tokens_conversation": "100",
		"max_conversation_turns": "5",
		"dm_auto_reply_message": "",
		"dm_auto_reply_enabled": "False",
		"ai_welcome_message_prompt": "Приветствуем в нашем чате! Рады видеть вас здесь. Расскажите немного о себе или задавайте вопросы!",
		"welcome_message_enabled": "False",
		"subscription_mode": "all_accounts",
		"subscription_accounts_per_entity": "5",
		"use_ai": "True"
	}
	for key, value in default_configs.items():
		if get_config_value(key) is None:
			set_config_value(key, value)
			logging.info(f"Установлено значение по умолчанию для '{key}': '{value}'")

	dp.include_router(task_management.router)
	dp.include_router(regular_comment.router)

	await set_bot_commands()
	await dp.start_polling(bot)


if __name__ == '__main__':
	logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
	logging.info("Запуск admin_bot в автономном режиме для тестирования...")
	asyncio.run(main_bot_polling_task())
