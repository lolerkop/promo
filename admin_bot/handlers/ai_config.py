# C:\Users\27030\Desktop\TEMP\admin_bot\handlers\ai_config.py
import html
import logging
from typing import cast

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from db import (add_openai_api_key, delete_openai_api_key, get_config_value,
                get_openai_api_keys, set_config_value,
                update_openai_api_key_status)

from ..bot_instance import (DEFAULT_AI_BASE_PROMPT, DEFAULT_AI_MAX_TOKENS,
                            DEFAULT_AI_TEMPERATURE, DEFAULT_OPENAI_MODEL, bot,
                            dp)
from ..keyboards import main_menu_keyboard
from ..states import AIConfigStates
from ..utils import user_is_allowed


def ai_config_menu_keyboard() -> InlineKeyboardMarkup:
	base_prompt = get_config_value("ai_base_prompt", DEFAULT_AI_BASE_PROMPT)
	openai_model = get_config_value("openai_model", DEFAULT_OPENAI_MODEL)
	temp = get_config_value("ai_temperature", DEFAULT_AI_TEMPERATURE)
	max_tokens = get_config_value("ai_max_tokens", DEFAULT_AI_MAX_TOKENS)

	conv_prompt = get_config_value("ai_base_prompt_conversation",
									 "Ты — дружелюбный и полезный ИИ-собеседник. Продолжай диалог естественно и по существу.")
	conv_max_tokens = get_config_value("ai_max_tokens_conversation", "100")
	conv_max_turns = get_config_value("max_conversation_turns", "5")

	welcome_prompt = get_config_value("ai_welcome_message_prompt", "Приветствуем!")
	welcome_enabled = get_config_value("welcome_message_enabled", "False").lower() == 'true'

	use_ai = get_config_value("use_ai", "True").lower() == 'true'

	use_ai_text = "🟢 С ИИ" if use_ai else "🔴 Без ИИ"
	welcome_status_text = "🟢 Включены" if welcome_enabled else "🔴 Выключены"

	buttons = [
		[InlineKeyboardButton(text="🔑 Управление API ключами OpenAI", callback_data="manage_openai_keys_menu")],
		[InlineKeyboardButton(text=use_ai_text, callback_data="change_ai_mode")],
		[
			InlineKeyboardButton(text=f"OpenAI модель: {openai_model}", callback_data="set_openai_model")
		],
		[
			InlineKeyboardButton(text=f"Температура: {temp}", callback_data="set_ai_temp")
		],
		[
			InlineKeyboardButton(text=f"Базовый промпт ({len(base_prompt)})", callback_data="set_ai_base_prompt"),
			InlineKeyboardButton(text=f"Макс. токены (коммент): {max_tokens}", callback_data="set_ai_max_tokens")
		],
		[
			InlineKeyboardButton(text=f"💬 Приветственный промпт ({len(welcome_prompt)})",
								 callback_data="set_ai_welcome_prompt"),
			InlineKeyboardButton(text=f"Статус приветствий: {welcome_status_text}",
								 callback_data="toggle_welcome_message_status")
		],
		[InlineKeyboardButton(text="--- Настройки диалогов ---", callback_data="ignore_dialogue_header")],
		[
			InlineKeyboardButton(text=f"Промпт диалога ({len(conv_prompt)})", callback_data="set_ai_conv_prompt"),
			InlineKeyboardButton(text=f"Макс.токены (диалог): {conv_max_tokens}", callback_data="set_ai_conv_tokens")
		],
		[
			InlineKeyboardButton(text=f"Макс. ходов (диалог): {conv_max_turns}", callback_data="set_ai_conv_turns")
		],
		[InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
	]
	return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "ai_config_menu")
async def cb_ai_config_menu(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	await callback.message.edit_text("🤖 <b>Настройки AI</b>\nВыберите параметр для изменения:",
									 reply_markup=ai_config_menu_keyboard())
	await callback.answer()


@dp.callback_query(F.data == "toggle_welcome_message_status")
async def cb_toggle_welcome_message_status(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return

	current_status_str = get_config_value("welcome_message_enabled", "False")
	new_status = "False" if current_status_str.lower() == 'true' else "True"
	set_config_value("welcome_message_enabled", new_status)

	await callback.message.edit_text("🤖 <b>Настройки AI</b>\nСтатус приветственных сообщений изменен.",
									 reply_markup=ai_config_menu_keyboard())
	await callback.answer(f"Приветственные сообщения теперь {'включены' if new_status == 'True' else 'выключены'}.")


def format_api_key_for_display(api_key: str) -> str:
	if not api_key or len(api_key) < 8:
		return "Некорректный ключ"
	return f"{api_key[:5]}...{api_key[-4:]}"


async def show_openai_keys_menu(message_or_callback: Message | CallbackQuery, state: FSMContext):
	chat_id = message_or_callback.chat.id if isinstance(message_or_callback,
														Message) else message_or_callback.message.chat.id

	keys = get_openai_api_keys()
	text = "🔑 <b>Управление API ключами OpenAI</b>\n\n"
	if not keys:
		text += "API ключи еще не добавлены."
	else:
		for key_data in keys:
			status_emoji = "🟢" if key_data['is_active'] else "🔴"
			label_str = f" ({html.escape(key_data['custom_label'])})" if key_data['custom_label'] else ""
			text += (f"{status_emoji} ID: {key_data['id']} | "
					 f"Ключ: <code>{format_api_key_for_display(key_data['api_key'])}</code>{label_str}\n"
					 f"  Использован: {key_data['last_used_timestamp'] or '-'}\n"
					 f"  Ошибка: {key_data['last_failed_timestamp'] or '-'} (Сбоев: {key_data['failure_count']})\n\n")

	kb_buttons = [
		[InlineKeyboardButton(text="➕ Добавить ключ", callback_data="add_openai_key_start")],
	]
	if keys:
		kb_buttons.append(
			[InlineKeyboardButton(text="🔄 Изменить статус ключа", callback_data="toggle_openai_key_start")])
		kb_buttons.append([InlineKeyboardButton(text="🗑️ Удалить ключ", callback_data="delete_openai_key_start")])

	kb_buttons.append([InlineKeyboardButton(text="⬅️ Назад в настройки AI", callback_data="ai_config_menu")])

	reply_markup = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

	if isinstance(message_or_callback, CallbackQuery):
		try:
			await message_or_callback.message.edit_text(text, reply_markup=reply_markup)
		except Exception:
			await bot.send_message(chat_id, text, reply_markup=reply_markup)
		await message_or_callback.answer()
	else:
		await message_or_callback.answer(text, reply_markup=reply_markup)


@dp.callback_query(F.data == "manage_openai_keys_menu")
async def cb_manage_openai_api_keys_menu(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	await state.clear()
	await show_openai_keys_menu(callback, state)


@dp.callback_query(F.data == "change_ai_mode")
async def change_ai_mode(callback: CallbackQuery):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	
	message = cast(Message, callback.message)
	current_value: bool = get_config_value("use_ai", "True").lower() == 'true'
	set_config_value("use_ai", str(not current_value))
	await message.edit_reply_markup(reply_markup=ai_config_menu_keyboard())


@dp.callback_query(F.data == "add_openai_key_start")
async def cb_add_openai_key_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	await callback.message.edit_text("Введите новый API ключ OpenAI (например, sk-xxxx...):\n"
									 "Вы также можете добавить метку для ключа через пробел после ключа (опционально).")
	await state.set_state(AIConfigStates.WaitingForNewOpenAIKey)
	await callback.answer()


@dp.message(AIConfigStates.WaitingForNewOpenAIKey, F.text, ~F.text.startswith('/'))
async def process_new_openai_key(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return

	parts = message.text.strip().split(maxsplit=1)
	api_key = parts[0]
	custom_label = parts[1] if len(parts) > 1 else None

	if not api_key.startswith("sk-") or len(api_key) < 20:
		await message.answer(
			"Неверный формат API ключа. Ключ должен начинаться с 'sk-' и быть достаточно длинным. Попробуйте снова.")
		return

	if add_openai_api_key(api_key, custom_label):
		await message.answer("✅ API ключ успешно добавлен.")
	else:
		await message.answer("⚠️ Этот API ключ уже существует в базе.")

	await state.clear()
	await show_openai_keys_menu(message, state)


@dp.callback_query(F.data == "delete_openai_key_start")
async def cb_delete_openai_key_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	keys = get_openai_api_keys()
	if not keys:
		await callback.answer("Нет ключей для удаления.", show_alert=True)
		return

	kb_buttons = []
	for key_data in keys:
		label_str = f" ({html.escape(key_data['custom_label'])})" if key_data['custom_label'] else ""
		kb_buttons.append([InlineKeyboardButton(
			text=f"ID: {key_data['id']} - {format_api_key_for_display(key_data['api_key'])}{label_str}",
			callback_data=f"confirm_del_openai_key:{key_data['id']}"
		)])
	kb_buttons.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="manage_openai_keys_menu")])

	await callback.message.edit_text("Выберите API ключ для удаления:",
									 reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))
	await state.set_state(AIConfigStates.ChoosingOpenAIKeyToDelete)
	await callback.answer()


@dp.callback_query(F.data.startswith("confirm_del_openai_key:"), AIConfigStates.ChoosingOpenAIKeyToDelete)
async def cb_confirm_delete_openai_key(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	try:
		key_id_to_delete = int(callback.data.split(":")[1])
	except (IndexError, ValueError):
		await callback.answer("Ошибка: неверный ID ключа.", show_alert=True)
		return

	if delete_openai_api_key(key_id_to_delete):
		await callback.answer("API ключ удален.", show_alert=True)
	else:
		await callback.answer("Не удалось удалить ключ (возможно, он уже удален).", show_alert=True)

	await state.clear()
	await show_openai_keys_menu(callback, state)


@dp.callback_query(F.data == "toggle_openai_key_start")
async def cb_toggle_openai_key_status_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	keys = get_openai_api_keys()
	if not keys:
		await callback.answer("Нет ключей для изменения статуса.", show_alert=True)
		return

	kb_buttons = []
	for key_data in keys:
		status_emoji = "🟢" if key_data['is_active'] else "🔴"
		action_text = "Деактивировать" if key_data['is_active'] else "Активировать"
		label_str = f" ({html.escape(key_data['custom_label'])})" if key_data['custom_label'] else ""
		kb_buttons.append([InlineKeyboardButton(
			text=f"{status_emoji} {action_text}: ID {key_data['id']} - {format_api_key_for_display(key_data['api_key'])}{label_str}",
			callback_data=f"confirm_toggle_openai_key:{key_data['id']}"
		)])
	kb_buttons.append([InlineKeyboardButton(text="⬅️ Отмена", callback_data="manage_openai_keys_menu")])

	await callback.message.edit_text("Выберите ключ для изменения статуса активности:",
									 reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))
	await state.set_state(AIConfigStates.ChoosingOpenAIKeyToToggle)
	await callback.answer()


@dp.callback_query(F.data.startswith("confirm_toggle_openai_key:"), AIConfigStates.ChoosingOpenAIKeyToToggle)
async def cb_confirm_toggle_openai_key_status(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	try:
		key_id_to_toggle = int(callback.data.split(":")[1])
	except (IndexError, ValueError):
		await callback.answer("Ошибка: неверный ID ключа.", show_alert=True)
		return

	keys = get_openai_api_keys()
	key_to_update = next((k for k in keys if k['id'] == key_id_to_toggle), None)

	if not key_to_update:
		await callback.answer("Ключ не найден.", show_alert=True)
		return

	new_status = not key_to_update['is_active']
	if update_openai_api_key_status(key_id_to_toggle, new_status):
		await callback.answer(
			f"Статус ключа ID {key_id_to_toggle} изменен на {'активен' if new_status else 'неактивен'}.",
			show_alert=True)
	else:
		await callback.answer("Не удалось изменить статус ключа.", show_alert=True)

	await state.clear()
	await show_openai_keys_menu(callback, state)


@dp.callback_query(F.data == "ignore_dialogue_header")
async def cb_ignore_dialogue_header(callback: CallbackQuery):
	await callback.answer()


@dp.callback_query(F.data == "set_ai_base_prompt")
async def cb_set_ai_base_prompt(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	current_val = get_config_value("ai_base_prompt", DEFAULT_AI_BASE_PROMPT)
	await callback.message.edit_text(
		f"Текущий базовый промпт (для комментариев):\n<pre>{html.escape(current_val)}</pre>\n\nВведите новый текст промпта:",
		parse_mode="HTML")
	await state.set_state(AIConfigStates.WaitingForBasePrompt)
	await callback.answer()


@dp.message(AIConfigStates.WaitingForBasePrompt, F.text, ~F.text.startswith('/'))
async def process_ai_base_prompt(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
	set_config_value("ai_base_prompt", message.text.strip())
	await message.answer("Базовый промпт (для комментариев) обновлен.", reply_markup=ai_config_menu_keyboard())
	await state.clear()


@dp.callback_query(F.data == "set_ai_welcome_prompt")
async def cb_set_ai_welcome_prompt(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	current_val = get_config_value("ai_welcome_message_prompt", "Приветствуем!")
	await callback.message.edit_text(
		f"Текущий приветственный промпт (для новых чатов):\n<pre>{html.escape(current_val)}</pre>\n\nВведите новый текст промпта:",
		parse_mode="HTML")
	await state.set_state(AIConfigStates.WaitingForWelcomePrompt)
	await callback.answer()


@dp.message(AIConfigStates.WaitingForWelcomePrompt, F.text, ~F.text.startswith('/'))
async def process_ai_welcome_prompt(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
	set_config_value("ai_welcome_message_prompt", message.text.strip())
	await message.answer("Приветственный промпт обновлен.", reply_markup=ai_config_menu_keyboard())
	await state.clear()


@dp.callback_query(F.data == "set_openai_model")
async def cb_set_openai_model(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	current_val = get_config_value("openai_model", DEFAULT_OPENAI_MODEL)
	await callback.message.edit_text(
		f"Текущая модель OpenAI: <code>{current_val}</code>\nВведите название новой модели (например, gpt-4, gpt-3.5-turbo):")
	await state.set_state(AIConfigStates.WaitingForOpenAIModel)
	await callback.answer()


@dp.message(AIConfigStates.WaitingForOpenAIModel, F.text, ~F.text.startswith('/'))
async def process_openai_model(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
	set_config_value("openai_model", message.text.strip())
	await message.answer("Модель OpenAI обновлена.", reply_markup=ai_config_menu_keyboard())
	await state.clear()


@dp.callback_query(F.data == "set_ai_temp")
async def cb_set_ai_temp(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	current_val = get_config_value("ai_temperature", DEFAULT_AI_TEMPERATURE)
	await callback.message.edit_text(
		f"Текущая температура: <code>{current_val}</code>\nВведите новое значение (например, 0.7):")
	await state.set_state(AIConfigStates.WaitingForTemperature)
	await callback.answer()


@dp.message(AIConfigStates.WaitingForTemperature, F.text, ~F.text.startswith('/'))
async def process_ai_temp(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
	try:
		val = float(message.text.strip())
		if 0.0 <= val <= 2.0:
			set_config_value("ai_temperature", str(val))
			await message.answer("Температура AI обновлена.", reply_markup=ai_config_menu_keyboard())
			await state.clear()
		else:
			await message.answer("Неверное значение. Температура должна быть между 0.0 и 2.0.")
	except ValueError:
		await message.answer("Неверный формат. Введите число (например, 0.7).")


@dp.callback_query(F.data == "set_ai_max_tokens")
async def cb_set_ai_max_tokens(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	current_val = get_config_value("ai_max_tokens", DEFAULT_AI_MAX_TOKENS)
	await callback.message.edit_text(
		f"Текущее макс. кол-во токенов (для комментариев): <code>{current_val}</code>\nВведите новое значение (например, 150):")
	await state.set_state(AIConfigStates.WaitingForMaxTokens)
	await callback.answer()


@dp.message(AIConfigStates.WaitingForMaxTokens, F.text, ~F.text.startswith('/'))
async def process_ai_max_tokens(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
	try:
		val = int(message.text.strip())
		if val > 0:
			set_config_value("ai_max_tokens", str(val))
			await message.answer("Макс. кол-во токенов AI (для комментариев) обновлено.",
								 reply_markup=ai_config_menu_keyboard())
			await state.clear()
		else:
			await message.answer("Неверное значение. Макс. кол-во токенов должно быть больше 0.")
	except ValueError:
		await message.answer("Неверный формат. Введите целое число (например, 150).")


@dp.callback_query(F.data == "set_ai_conv_prompt")
async def cb_set_ai_conv_prompt(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	current_val = get_config_value("ai_base_prompt_conversation",
									 "Ты — дружелюбный и полезный ИИ-собеседник. Продолжай диалог естественно и по существу.")
	await callback.message.edit_text(
		f"Текущий базовый промпт (для диалогов):\n<pre>{html.escape(current_val)}</pre>\n\nВведите новый текст промпта:",
		parse_mode="HTML")
	await state.set_state(AIConfigStates.WaitingForConvPrompt)
	await callback.answer()


@dp.message(AIConfigStates.WaitingForConvPrompt, F.text, ~F.text.startswith('/'))
async def process_ai_conv_prompt(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
	set_config_value("ai_base_prompt_conversation", message.text.strip())
	await message.answer("Базовый промпт (для диалогов) обновлен.", reply_markup=ai_config_menu_keyboard())
	await state.clear()


@dp.callback_query(F.data == "set_ai_conv_tokens")
async def cb_set_ai_conv_tokens(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	current_val = get_config_value("ai_max_tokens_conversation", "100")
	await callback.message.edit_text(
		f"Текущее макс. кол-во токенов (для диалогов): <code>{current_val}</code>\nВведите новое значение (например, 100):")
	await state.set_state(AIConfigStates.WaitingForConvMaxTokens)
	await callback.answer()


@dp.message(AIConfigStates.WaitingForConvMaxTokens, F.text, ~F.text.startswith('/'))
async def process_ai_conv_tokens(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
	try:
		val = int(message.text.strip())
		if val > 0:
			set_config_value("ai_max_tokens_conversation", str(val))
			await message.answer("Макс. кол-во токенов AI (для диалогов) обновлено.",
								 reply_markup=ai_config_menu_keyboard())
			await state.clear()
		else:
			await message.answer("Неверное значение. Макс. кол-во токенов должно быть больше 0.")
	except ValueError:
		await message.answer("Неверный формат. Введите целое число (например, 100).")


@dp.callback_query(F.data == "set_ai_conv_turns")
async def cb_set_ai_conv_turns(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
	current_val = get_config_value("max_conversation_turns", "5")
	await callback.message.edit_text(
		f"Текущее макс. кол-во ходов в диалоге: <code>{current_val}</code>\nВведите новое значение (например, 5):")
	await state.set_state(AIConfigStates.WaitingForConvMaxTurns)
	await callback.answer()


@dp.message(AIConfigStates.WaitingForConvMaxTurns, F.text, ~F.text.startswith('/'))
async def process_ai_conv_turns(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id): await message.answer("Нет прав."); return
	try:
		val = int(message.text.strip())
		if val >= 0:
			set_config_value("max_conversation_turns", str(val))
			await message.answer("Макс. кол-во ходов в диалоге обновлено.", reply_markup=ai_config_menu_keyboard())
			await state.clear()
		else:
			await message.answer("Неверное значение. Количество ходов должно быть 0 или больше.")
	except ValueError:
		await message.answer("Неверный формат. Введите целое число (например, 5).")
