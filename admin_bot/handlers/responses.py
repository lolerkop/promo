import html

from aiogram import F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (
	BufferedInputFile,
	CallbackQuery,
	InlineKeyboardButton,
	InlineKeyboardMarkup,
	Message,
)

from db import get_config_value, get_db_connection, set_config_value

from ..bot_instance import dp
from ..keyboards import cancel_action_keyboard, main_menu_keyboard
from ..prompt_files import read_prompt_document, send_prompt_preview_file
from ..utils import user_is_allowed
from services.prompt_text import PromptTextError
from services.trigger_engine import (
	build_full_trigger_export_text,
	delete_intent_phrase,
	delete_trigger_intent,
	ensure_default_vpn_intents,
	evaluate_trigger_for_message,
	fetch_trigger_intents,
	get_intent_phrase,
	get_trigger_intent,
	import_global_triggers_from_text,
	semantic_analysis_enabled,
	set_trigger_semantic_enabled,
	update_trigger_intent,
	upsert_trigger_intent,
	add_intent_phrases,
)


DEFAULT_TRIGGER_AI_PROMPT = (
	"Ты отвечаешь реплаем в Telegram-чате как обычный живой человек. "
	"Твоя задача - коротко и естественно отреагировать на сообщение, где человеку может быть нужен VPN. "
	"Не пиши как бот, не начинай с приветствия, не веди долгий диалог. "
	"Ответ должен мягко подвести к VPN-сервису, но без агрессивной рекламы. "
	"Если человек жалуется на блокировки, связь, глушилки или неработающие сервисы - покажи понимание и предложи VPN как практичное решение. "
	"Пиши 1-2 коротких предложения."
)

DEFAULT_TRIGGER_AI_MODEL = "gpt-4o-mini"
DEFAULT_TRIGGER_AI_TEMPERATURE = "0.8"
DEFAULT_TRIGGER_AI_MAX_TOKENS = "120"


def _response_type_label(response_type: str | None) -> str:
	return "AI" if response_type == "openai" else "текст"


def _fetch_responses() -> list[dict]:
	conn = get_db_connection()
	cursor = conn.cursor()
	cursor.execute("""
		SELECT rowid AS rid, keyword, answer, response_type
		FROM responses
		ORDER BY keyword
	""")
	rows = [dict(row) for row in cursor.fetchall()]
	conn.close()
	return rows


def _get_response_by_id(response_id: int) -> dict | None:
	conn = get_db_connection()
	cursor = conn.cursor()
	cursor.execute("""
		SELECT rowid AS rid, keyword, answer, response_type
		FROM responses
		WHERE rowid = ?
	""", (response_id,))
	row = cursor.fetchone()
	conn.close()
	return dict(row) if row else None


def _save_response(keyword: str, answer: str, response_type: str):
	conn = get_db_connection()
	cursor = conn.cursor()
	keyword = keyword.strip().lower()
	cursor.execute(
		"UPDATE responses SET answer = ?, response_type = ? WHERE keyword = ?",
		(answer or "", response_type, keyword)
	)
	if cursor.rowcount == 0:
		cursor.execute(
			"INSERT INTO responses (keyword, answer, response_type) VALUES (?, ?, ?)",
			(keyword, answer or "", response_type)
		)
	conn.commit()
	conn.close()


def _delete_response(response_id: int) -> bool:
	conn = get_db_connection()
	cursor = conn.cursor()
	cursor.execute("DELETE FROM responses WHERE rowid = ?", (response_id,))
	deleted = cursor.rowcount > 0
	conn.commit()
	conn.close()
	return deleted


def _set_all_responses_ai() -> int:
	conn = get_db_connection()
	cursor = conn.cursor()
	cursor.execute("UPDATE responses SET answer = '', response_type = 'openai'")
	updated = cursor.rowcount
	conn.commit()
	conn.close()
	return updated


def _delete_all_responses() -> int:
	conn = get_db_connection()
	cursor = conn.cursor()
	cursor.execute("DELETE FROM responses")
	deleted = cursor.rowcount
	conn.commit()
	conn.close()
	return deleted


def _response_stats(responses: list[dict]) -> tuple[int, int, int]:
	total = len(responses)
	ai_count = sum(1 for item in responses if item.get("response_type") == "openai")
	text_count = total - ai_count
	return total, ai_count, text_count


def _get_trigger_ai_prompt() -> str:
	return get_config_value("ai_trigger_prompt", DEFAULT_TRIGGER_AI_PROMPT)


def _get_trigger_ai_model() -> str:
	return get_config_value("ai_trigger_model", get_config_value("openai_model", DEFAULT_TRIGGER_AI_MODEL))


def _get_trigger_ai_temperature() -> str:
	return get_config_value("ai_trigger_temperature", DEFAULT_TRIGGER_AI_TEMPERATURE)


def _get_trigger_ai_max_tokens() -> str:
	return get_config_value("ai_trigger_max_tokens", DEFAULT_TRIGGER_AI_MAX_TOKENS)


def _get_semantic_threshold() -> str:
	return get_config_value("trigger_semantic_threshold", "0.72")


def _get_semantic_model() -> str:
	return get_config_value("trigger_semantic_model", _get_trigger_ai_model())


def _get_semantic_rate_limit() -> str:
	return get_config_value("trigger_semantic_max_per_minute", "30")


def _get_chat_cooldown() -> str:
	return get_config_value("trigger_chat_cooldown_seconds", "120")


def _get_user_cooldown() -> str:
	return get_config_value("trigger_user_cooldown_seconds", "300")


def _trigger_ai_settings_text() -> str:
	prompt = _get_trigger_ai_prompt()
	semantic_status = "включен" if semantic_analysis_enabled() else "выключен"
	return (
		"<b>AI-настройки триггеров</b>\n\n"
		f"Модель: <code>{html.escape(_get_trigger_ai_model())}</code>\n"
		f"Температура: <b>{html.escape(_get_trigger_ai_temperature())}</b>\n"
		f"Макс. токены: <b>{html.escape(_get_trigger_ai_max_tokens())}</b>\n"
		f"Промпт: <b>{len(prompt)}</b> симв.\n\n"
		"<b>AI-анализ смысла</b>\n"
		f"Статус: <b>{semantic_status}</b>\n"
		f"Модель классификации: <code>{html.escape(_get_semantic_model())}</code>\n"
		f"Порог уверенности: <b>{html.escape(_get_semantic_threshold())}</b>\n"
		f"Проверок в минуту: <b>{html.escape(_get_semantic_rate_limit())}</b>\n"
		f"Cooldown чата: <b>{html.escape(_get_chat_cooldown())}</b> сек.\n"
		f"Cooldown пользователя: <b>{html.escape(_get_user_cooldown())}</b> сек.\n\n"
		"Эти настройки используются только для ответов на триггеры. "
		"Обычные автокомментарии и диалоги остаются на своих AI-настройках."
	)


def _trigger_ai_settings_keyboard() -> InlineKeyboardMarkup:
	semantic_button = "Выключить AI-смысл" if semantic_analysis_enabled() else "Включить AI-смысл"
	return InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Промпт триггеров", callback_data="response_ai_prompt")],
		[InlineKeyboardButton(text=f"Модель: {_get_trigger_ai_model()}", callback_data="response_ai_model")],
		[
			InlineKeyboardButton(text=f"Температура: {_get_trigger_ai_temperature()}", callback_data="response_ai_temperature"),
			InlineKeyboardButton(text=f"Токены: {_get_trigger_ai_max_tokens()}", callback_data="response_ai_max_tokens"),
		],
		[InlineKeyboardButton(text=semantic_button, callback_data="response_semantic_toggle")],
		[
			InlineKeyboardButton(text=f"Порог: {_get_semantic_threshold()}", callback_data="response_semantic_threshold"),
			InlineKeyboardButton(text=f"Классификатор: {_get_semantic_model()}", callback_data="response_semantic_model"),
		],
		[
			InlineKeyboardButton(text=f"Лимит/мин: {_get_semantic_rate_limit()}", callback_data="response_semantic_rate"),
			InlineKeyboardButton(text=f"Cooldown: {_get_chat_cooldown()}/{_get_user_cooldown()}", callback_data="response_cooldowns"),
		],
		[InlineKeyboardButton(text="Назад к триггерам", callback_data="set_answer")],
	])


def _build_responses_file_text(responses: list[dict]) -> str:
	total, ai_count, text_count = _response_stats(responses)
	lines = [
		"Список триггеров",
		f"Всего: {total}",
		f"AI: {ai_count}",
		f"Готовый текст: {text_count}",
		"",
	]
	for index, item in enumerate(responses, start=1):
		keyword = str(item.get("keyword") or "").strip()
		response_type = _response_type_label(item.get("response_type"))
		lines.append(f"{index}. [{response_type}] {keyword}")
	return "\n".join(lines).strip() + "\n"


async def _send_responses_file(message: Message, responses: list[dict]) -> None:
	if not responses:
		return
	document = BufferedInputFile(
		_build_responses_file_text(responses).encode("utf-8"),
		filename="triggers.txt",
	)
	await message.answer_document(
		document=document,
		caption="Список триггеров отправил файлом, чтобы меню не превращалось в простыню."
	)


async def _send_full_triggers_file(message: Message) -> None:
	document = BufferedInputFile(
		build_full_trigger_export_text().encode("utf-8"),
		filename="advanced_triggers.txt",
	)
	await message.answer_document(
		document=document,
		caption="Полный экспорт триггеров и намерений."
	)


def _intents_text() -> str:
	intents = fetch_trigger_intents(include_phrases=True)
	if not intents:
		return "<b>Намерения</b>\n\nПока нет намерений."
	lines = ["<b>Намерения</b>", ""]
	for intent in intents[:30]:
		status = "вкл" if int(intent.get("is_active") or 0) else "выкл"
		semantic = "AI-смысл" if int(intent.get("semantic_enabled") or 0) else "без AI-смысла"
		lines.append(
			f"• <b>{html.escape(intent['name'])}</b> [{status}, {semantic}] - "
			f"{len(intent.get('phrases', []))} фраз"
		)
	if len(intents) > 30:
		lines.append(f"\n...и еще {len(intents) - 30}")
	return "\n".join(lines)


def _intents_keyboard() -> InlineKeyboardMarkup:
	buttons = []
	for intent in fetch_trigger_intents(include_phrases=False)[:25]:
		status = "🟢" if int(intent.get("is_active") or 0) else "⚪"
		semantic = "AI" if int(intent.get("semantic_enabled") or 0) else "ключи"
		name = str(intent.get("name") or "Без названия")
		if len(name) > 32:
			name = name[:29] + "..."
		buttons.append([
			InlineKeyboardButton(
				text=f"{status} {name} ({semantic})",
				callback_data=f"response_intent_view:{intent['id']}",
			)
		])
	buttons.extend([
		[InlineKeyboardButton(text="Создать базовые VPN-намерения", callback_data="response_intents_defaults")],
		[InlineKeyboardButton(text="Добавить намерение", callback_data="response_intent_add_start")],
		[InlineKeyboardButton(text="Скачать полный .txt", callback_data="response_full_export_txt")],
		[InlineKeyboardButton(text="Назад", callback_data="set_answer")],
	])
	return InlineKeyboardMarkup(inline_keyboard=buttons)


def _intent_detail_text(intent: dict) -> str:
	status = "включено" if int(intent.get("is_active") or 0) else "выключено"
	semantic = "включен" if int(intent.get("semantic_enabled") or 0) else "выключен"
	response_type = _response_type_label(intent.get("response_type"))
	answer = intent.get("answer") or ""
	answer_text = "AI генерирует ответ по промпту триггеров."
	if intent.get("response_type") == "predefined":
		answer_text = answer or "-"
	phrases = intent.get("phrases") or []
	preview_phrases = "\n".join(f"• {html.escape(item['phrase'])}" for item in phrases[:12])
	if len(phrases) > 12:
		preview_phrases += f"\n...и еще {len(phrases) - 12}"
	if not preview_phrases:
		preview_phrases = "-"
	return (
		f"<b>Намерение:</b> {html.escape(intent.get('name') or '-')}\n"
		f"<b>ID:</b> <code>{intent.get('id')}</code>\n"
		f"<b>Статус:</b> {status}\n"
		f"<b>AI-смысл:</b> {semantic}\n"
		f"<b>Тип ответа:</b> {response_type}\n"
		f"<b>Фраз:</b> {len(phrases)}\n\n"
		f"<b>Описание:</b>\n{html.escape(intent.get('description') or '-')}\n\n"
		f"<b>Ответ:</b>\n<pre>{html.escape(answer_text[:900])}</pre>\n\n"
		f"<b>Фразы:</b>\n{preview_phrases}"
	)


def _intent_detail_keyboard(intent: dict) -> InlineKeyboardMarkup:
	intent_id = intent["id"]
	active_text = "Выключить намерение" if int(intent.get("is_active") or 0) else "Включить намерение"
	semantic_text = "Выключить AI-смысл" if int(intent.get("semantic_enabled") or 0) else "Включить AI-смысл"
	return InlineKeyboardMarkup(inline_keyboard=[
		[
			InlineKeyboardButton(text=active_text, callback_data=f"response_intent_toggle:{intent_id}"),
			InlineKeyboardButton(text=semantic_text, callback_data=f"response_intent_toggle_semantic:{intent_id}"),
		],
		[
			InlineKeyboardButton(text="Переименовать", callback_data=f"response_intent_rename_start:{intent_id}"),
			InlineKeyboardButton(text="Описание", callback_data=f"response_intent_desc_start:{intent_id}"),
		],
		[
			InlineKeyboardButton(text="Ответ: AI", callback_data=f"response_intent_set_ai:{intent_id}"),
			InlineKeyboardButton(text="Ответ: текст", callback_data=f"response_intent_answer_start:{intent_id}"),
		],
		[
			InlineKeyboardButton(text="Добавить фразы", callback_data=f"response_intent_add_phrases_start:{intent_id}"),
			InlineKeyboardButton(text="Фразы/удаление", callback_data=f"response_intent_phrases:{intent_id}"),
		],
		[InlineKeyboardButton(text="Удалить намерение", callback_data=f"response_intent_delete_confirm:{intent_id}")],
		[InlineKeyboardButton(text="К намерениям", callback_data="response_intents")],
	])


def _intent_phrases_text(intent: dict) -> str:
	phrases = intent.get("phrases") or []
	lines = [
		f"<b>Фразы намерения:</b> {html.escape(intent.get('name') or '-')}",
		f"Всего: <b>{len(phrases)}</b>",
		"",
		"Нажмите на фразу, чтобы удалить ее.",
	]
	for index, phrase in enumerate(phrases[:35], start=1):
		lines.append(f"{index}. <code>{html.escape(phrase['phrase'])}</code>")
	if len(phrases) > 35:
		lines.append(f"\n...и еще {len(phrases) - 35}. Полный список есть в экспорте.")
	return "\n".join(lines)


def _intent_phrases_keyboard(intent: dict) -> InlineKeyboardMarkup:
	intent_id = intent["id"]
	buttons = []
	for phrase in (intent.get("phrases") or [])[:35]:
		label = str(phrase.get("phrase") or "")
		if len(label) > 42:
			label = label[:39] + "..."
		buttons.append([
			InlineKeyboardButton(
				text=f"Удалить: {label}",
				callback_data=f"response_intent_phrase_delete_confirm:{phrase['id']}:{intent_id}",
			)
		])
	buttons.append([InlineKeyboardButton(text="Добавить фразы", callback_data=f"response_intent_add_phrases_start:{intent_id}")])
	buttons.append([InlineKeyboardButton(text="Назад к намерению", callback_data=f"response_intent_view:{intent_id}")])
	return InlineKeyboardMarkup(inline_keyboard=buttons)


def _import_export_keyboard() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Импорт ключевых фраз .txt", callback_data="response_import_txt_start")],
		[InlineKeyboardButton(text="Экспорт всего .txt", callback_data="response_full_export_txt")],
		[InlineKeyboardButton(text="Назад", callback_data="set_answer")],
	])


def responses_menu_keyboard() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Намерения", callback_data="response_intents")],
		[InlineKeyboardButton(text="Ключевые фразы", callback_data="response_list")],
		[InlineKeyboardButton(text="AI-настройки триггеров", callback_data="response_ai_settings")],
		[
			InlineKeyboardButton(text="Импорт/экспорт .txt", callback_data="response_import_export"),
			InlineKeyboardButton(text="Тест триггера", callback_data="response_test_start"),
		],
		[InlineKeyboardButton(text="Назад в главное меню", callback_data="back_to_main_menu")]
	])


def _responses_menu_text() -> str:
	responses = _fetch_responses()
	total, ai_count, text_count = _response_stats(responses)
	intents = fetch_trigger_intents(include_phrases=True)
	intent_phrases = sum(len(item.get("phrases", [])) for item in intents)
	listen_all = get_config_value("listen_all", "True").lower() == "true"
	tracking_text = "все чаты" if listen_all else "только включенные группы/категории из БД"
	semantic_status = "включен" if semantic_analysis_enabled() else "выключен"
	return (
		"<b>Ответы на триггеры</b>\n\n"
		f"Ключевых фраз: <b>{total}</b> (AI: <b>{ai_count}</b>, текст: <b>{text_count}</b>)\n"
		f"Намерений: <b>{len(intents)}</b>, фраз в намерениях: <b>{intent_phrases}</b>\n"
		f"AI-анализ смысла: <b>{semantic_status}</b>\n"
		f"Режим отслеживания: <b>{tracking_text}</b>\n\n"
		"Сначала бот ищет локальные совпадения, потом при включенной настройке может проверить смысл сообщения через AI."
	)


def _responses_list_keyboard(has_responses: bool = True) -> InlineKeyboardMarkup:
	buttons = []
	if has_responses:
		buttons.append([InlineKeyboardButton(text="Скачать список .txt", callback_data="response_export_txt")])
		buttons.append([InlineKeyboardButton(text="Все триггеры в AI-режим", callback_data="response_all_ai_confirm")])
		buttons.append([InlineKeyboardButton(text="Удалить все триггеры", callback_data="response_delete_all_confirm")])
	buttons.append([InlineKeyboardButton(text="AI-настройки триггеров", callback_data="response_ai_settings")])
	buttons.append([InlineKeyboardButton(text="Добавить / обновить триггер", callback_data="response_add_start")])
	buttons.append([InlineKeyboardButton(text="Назад", callback_data="set_answer")])
	return InlineKeyboardMarkup(inline_keyboard=buttons)


def _response_detail_keyboard(response_id: int) -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Изменить текст", callback_data=f"response_edit_text:{response_id}")],
		[InlineKeyboardButton(text="Переключить в AI-режим", callback_data=f"response_set_ai:{response_id}")],
		[InlineKeyboardButton(text="Удалить", callback_data=f"response_delete_confirm:{response_id}")],
		[InlineKeyboardButton(text="К списку", callback_data="response_list")]
	])


def _format_response_detail(item: dict) -> str:
	answer = item.get("answer") or ""
	answer_text = html.escape(answer[:1000]) if answer else "-"
	if item.get("response_type") == "openai":
		answer_text = "AI генерирует ответ по тексту сообщения."
	return (
		f"<b>Триггер:</b> <code>{html.escape(item['keyword'])}</code>\n"
		f"<b>Тип:</b> {_response_type_label(item.get('response_type'))}\n\n"
		f"<b>Ответ:</b>\n<pre>{answer_text}</pre>"
	)


@dp.callback_query(F.data == "set_answer")
async def cb_set_answer(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	await callback.message.edit_text(_responses_menu_text(), reply_markup=responses_menu_keyboard())
	await callback.answer()


@dp.message(Command("responses"))
async def cmd_responses(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	await state.clear()
	await message.answer(_responses_menu_text(), reply_markup=responses_menu_keyboard())


@dp.callback_query(F.data == "response_add_start")
async def cb_response_add_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	await callback.message.edit_text(
		"Введите ключевое слово или фразу.\n\n"
		"Пример: <code>привет</code>, <code>заказ</code>, <code>нужен vpn</code>.\n"
		"Регистр не важен.",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state("waiting_for_keyword")
	await callback.answer()


@dp.callback_query(F.data == "response_list")
async def cb_response_list(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	responses = _fetch_responses()
	if not responses:
		await callback.message.edit_text(
			"<b>Список триггеров</b>\n\nПока нет ни одного триггера.",
			reply_markup=_responses_list_keyboard(has_responses=False)
		)
	else:
		total, ai_count, text_count = _response_stats(responses)
		await callback.message.edit_text(
			"<b>Список триггеров</b>\n\n"
			f"Всего: <b>{total}</b>\n"
			f"AI-ответов: <b>{ai_count}</b>\n"
			f"Готовых текстов: <b>{text_count}</b>\n\n"
			"Полный список отправляю отдельным .txt файлом.",
			reply_markup=_responses_list_keyboard(has_responses=True)
		)
		await _send_responses_file(callback.message, responses)
	await callback.answer()


@dp.callback_query(F.data == "response_export_txt")
async def cb_response_export_txt(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	responses = _fetch_responses()
	if not responses:
		await callback.answer("Список пуст.", show_alert=True)
		return
	await _send_responses_file(callback.message, responses)
	await callback.answer("Файл отправлен.")


@dp.callback_query(F.data == "response_full_export_txt")
async def cb_response_full_export_txt(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	await _send_full_triggers_file(callback.message)
	await callback.answer("Файл отправлен.")


@dp.callback_query(F.data == "response_import_export")
async def cb_response_import_export(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	await callback.message.edit_text(
		"<b>Импорт/экспорт триггеров</b>\n\n"
		"Импорт .txt добавляет каждую непустую строку как глобальную ключевую фразу в AI-режиме.\n"
		"Формат для готового текста: <code>фраза => ответ</code>.",
		reply_markup=_import_export_keyboard()
	)
	await callback.answer()


@dp.callback_query(F.data == "response_import_txt_start")
async def cb_response_import_txt_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	await callback.message.edit_text(
		"Отправьте .txt файл со списком фраз.\n\n"
		"Каждая строка - отдельный триггер. Пустые строки и строки с # игнорируются.\n"
		"Для готового ответа можно писать: <code>фраза => ответ</code>.",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state("waiting_for_trigger_import_txt")
	await callback.answer()


@dp.message(StateFilter("waiting_for_trigger_import_txt"), F.document)
async def handle_trigger_import_document(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	try:
		text = await read_prompt_document(message.document)
	except PromptTextError as exc:
		await message.answer(f"Импорт не выполнен: {html.escape(str(exc))}", reply_markup=cancel_action_keyboard())
		return
	added, updated = import_global_triggers_from_text(text)
	await state.clear()
	await message.answer(
		f"Импорт завершен.\nДобавлено: <b>{added}</b>\nОбновлено: <b>{updated}</b>",
		reply_markup=responses_menu_keyboard()
	)


@dp.message(StateFilter("waiting_for_trigger_import_txt"), F.text, ~F.text.startswith('/'))
async def handle_trigger_import_text(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	added, updated = import_global_triggers_from_text(message.text)
	await state.clear()
	await message.answer(
		f"Импорт завершен.\nДобавлено: <b>{added}</b>\nОбновлено: <b>{updated}</b>",
		reply_markup=responses_menu_keyboard()
	)


@dp.callback_query(F.data == "response_test_start")
async def cb_response_test_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	await callback.message.edit_text(
		"Отправьте пример сообщения из чата.\n\n"
		"Бот покажет, какой триггер или смысловое намерение сработает. "
		"Сообщение никуда отправляться не будет.",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state("waiting_for_trigger_test_text")
	await callback.answer()


@dp.message(StateFilter("waiting_for_trigger_test_text"), F.text, ~F.text.startswith('/'))
async def handle_trigger_test_text(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	decision = evaluate_trigger_for_message(
		message.text,
		chat_id=message.chat.id,
		sender_id=message.from_user.id,
		category_name=None,
		allow_global=True,
		allow_semantic=True,
		respect_cooldown=False,
	)
	if not decision.matched:
		text = (
			"<b>Тест триггера</b>\n\n"
			"Совпадение не найдено.\n"
			f"Причина: <code>{html.escape(decision.reason or 'no_match')}</code>"
		)
	else:
		answer_preview = decision.answer or "AI сгенерирует ответ по промпту триггеров."
		text = (
			"<b>Тест триггера</b>\n\n"
			f"Источник: <b>{html.escape(decision.source)}</b>\n"
			f"Тип совпадения: <b>{html.escape(decision.match_type)}</b>\n"
			f"Ключ: <code>{html.escape(decision.keyword or '-')}</code>\n"
			f"Намерение: <b>{html.escape(decision.intent_name or '-')}</b>\n"
			f"Тип ответа: <b>{html.escape(decision.response_type or '-')}</b>\n"
			f"Уверенность: <b>{decision.confidence:.2f}</b>\n"
			f"Причина: <code>{html.escape(decision.reason or '-')}</code>\n\n"
			f"<b>Ответ:</b>\n<pre>{html.escape(answer_preview[:800])}</pre>"
		)
	await state.clear()
	await message.answer(text, reply_markup=responses_menu_keyboard())


@dp.callback_query(F.data == "response_intents")
async def cb_response_intents(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	await callback.message.edit_text(_intents_text(), reply_markup=_intents_keyboard())
	await callback.answer()


@dp.callback_query(F.data.startswith("response_intent_view:"))
async def cb_response_intent_view(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	try:
		intent_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	intent = get_trigger_intent(intent_id)
	if not intent:
		await callback.answer("Намерение не найдено.", show_alert=True)
		await cb_response_intents(callback, state)
		return
	await callback.message.edit_text(_intent_detail_text(intent), reply_markup=_intent_detail_keyboard(intent))
	await callback.answer()


@dp.callback_query(F.data.startswith("response_intent_toggle:"))
async def cb_response_intent_toggle(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		intent_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	intent = get_trigger_intent(intent_id)
	if not intent:
		await callback.answer("Намерение не найдено.", show_alert=True)
		return
	updated = update_trigger_intent(intent_id, is_active=0 if int(intent.get("is_active") or 0) else 1)
	await callback.message.edit_text(_intent_detail_text(updated), reply_markup=_intent_detail_keyboard(updated))
	await callback.answer("Статус обновлен.")


@dp.callback_query(F.data.startswith("response_intent_toggle_semantic:"))
async def cb_response_intent_toggle_semantic(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		intent_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	intent = get_trigger_intent(intent_id)
	if not intent:
		await callback.answer("Намерение не найдено.", show_alert=True)
		return
	updated = update_trigger_intent(intent_id, semantic_enabled=0 if int(intent.get("semantic_enabled") or 0) else 1)
	await callback.message.edit_text(_intent_detail_text(updated), reply_markup=_intent_detail_keyboard(updated))
	await callback.answer("AI-смысл обновлен.")


@dp.callback_query(F.data.startswith("response_intent_rename_start:"))
async def cb_response_intent_rename_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		intent_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	intent = get_trigger_intent(intent_id, include_phrases=False)
	if not intent:
		await callback.answer("Намерение не найдено.", show_alert=True)
		return
	await state.update_data(intent_id=intent_id)
	await callback.message.edit_text(
		f"Текущее название: <b>{html.escape(intent.get('name') or '-')}</b>\n\nВведите новое название:",
		reply_markup=cancel_action_keyboard(),
	)
	await state.set_state("waiting_for_intent_rename")
	await callback.answer()


@dp.message(StateFilter("waiting_for_intent_rename"), F.text, ~F.text.startswith('/'))
async def handle_intent_rename(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	data = await state.get_data()
	intent_id = data.get("intent_id")
	name = message.text.strip()
	if not intent_id or not name:
		await message.answer("Название не может быть пустым.", reply_markup=cancel_action_keyboard())
		return
	try:
		intent = update_trigger_intent(int(intent_id), name=name)
	except Exception as exc:
		await message.answer(f"Не удалось переименовать: {html.escape(str(exc))}", reply_markup=cancel_action_keyboard())
		return
	await state.clear()
	await message.answer(_intent_detail_text(intent), reply_markup=_intent_detail_keyboard(intent))


@dp.callback_query(F.data.startswith("response_intent_desc_start:"))
async def cb_response_intent_desc_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		intent_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	intent = get_trigger_intent(intent_id, include_phrases=False)
	if not intent:
		await callback.answer("Намерение не найдено.", show_alert=True)
		return
	await state.update_data(intent_id=intent_id)
	await callback.message.edit_text(
		f"Текущее описание:\n<pre>{html.escape(intent.get('description') or '-')}</pre>\n\n"
		"Введите новое описание смысла намерения. Для очистки отправьте <code>-</code>.",
		reply_markup=cancel_action_keyboard(),
	)
	await state.set_state("waiting_for_intent_description_edit")
	await callback.answer()


@dp.message(StateFilter("waiting_for_intent_description_edit"), F.text, ~F.text.startswith('/'))
async def handle_intent_description_edit(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	data = await state.get_data()
	intent_id = data.get("intent_id")
	description = "" if message.text.strip() == "-" else message.text.strip()
	intent = update_trigger_intent(int(intent_id), description=description)
	await state.clear()
	await message.answer(_intent_detail_text(intent), reply_markup=_intent_detail_keyboard(intent))


@dp.callback_query(F.data.startswith("response_intent_set_ai:"))
async def cb_response_intent_set_ai(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		intent_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	intent = update_trigger_intent(intent_id, response_type="openai", answer="")
	if not intent:
		await callback.answer("Намерение не найдено.", show_alert=True)
		return
	await callback.message.edit_text(_intent_detail_text(intent), reply_markup=_intent_detail_keyboard(intent))
	await callback.answer("Ответ переключен в AI.")


@dp.callback_query(F.data.startswith("response_intent_answer_start:"))
async def cb_response_intent_answer_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		intent_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	intent = get_trigger_intent(intent_id, include_phrases=False)
	if not intent:
		await callback.answer("Намерение не найдено.", show_alert=True)
		return
	await state.update_data(intent_id=intent_id)
	await callback.message.edit_text(
		"Введите готовый текст ответа для этого намерения.\n\n"
		"После сохранения намерение будет отвечать этим текстом вместо AI-генерации.",
		reply_markup=cancel_action_keyboard(),
	)
	await state.set_state("waiting_for_intent_answer")
	await callback.answer()


@dp.message(StateFilter("waiting_for_intent_answer"), F.text, ~F.text.startswith('/'))
async def handle_intent_answer(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	data = await state.get_data()
	intent_id = data.get("intent_id")
	answer = message.text.strip()
	if not answer:
		await message.answer("Ответ не может быть пустым.", reply_markup=cancel_action_keyboard())
		return
	intent = update_trigger_intent(int(intent_id), response_type="predefined", answer=answer)
	await state.clear()
	await message.answer(_intent_detail_text(intent), reply_markup=_intent_detail_keyboard(intent))


@dp.callback_query(F.data.startswith("response_intent_add_phrases_start:"))
async def cb_response_intent_add_phrases_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		intent_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	intent = get_trigger_intent(intent_id, include_phrases=False)
	if not intent:
		await callback.answer("Намерение не найдено.", show_alert=True)
		return
	await state.update_data(intent_id=intent_id)
	await callback.message.edit_text(
		"Отправьте фразы для намерения: текстом или .txt файлом.\n\n"
		"Каждая строка - отдельная фраза. Пустые строки и строки с # игнорируются.",
		reply_markup=cancel_action_keyboard(),
	)
	await state.set_state("waiting_for_intent_add_phrases")
	await callback.answer()


def _parse_phrase_lines(text: str) -> list[str]:
	return [line.strip() for line in (text or "").splitlines() if line.strip() and not line.strip().startswith("#")]


async def _save_intent_phrases_from_text(message: Message, state: FSMContext, text: str):
	data = await state.get_data()
	intent_id = data.get("intent_id")
	if not intent_id:
		await message.answer("Намерение не найдено. Начните заново.", reply_markup=responses_menu_keyboard())
		await state.clear()
		return
	phrases = _parse_phrase_lines(text)
	if not phrases:
		await message.answer("Фразы не найдены.", reply_markup=cancel_action_keyboard())
		return
	added = add_intent_phrases(int(intent_id), phrases)
	intent = get_trigger_intent(int(intent_id))
	await state.clear()
	await message.answer(
		f"Фразы обработаны: <b>{len(phrases)}</b>\nДобавлено новых: <b>{added}</b>\n\n"
		+ _intent_detail_text(intent),
		reply_markup=_intent_detail_keyboard(intent),
	)


@dp.message(StateFilter("waiting_for_intent_add_phrases"), F.text, ~F.text.startswith('/'))
async def handle_intent_add_phrases_text(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	await _save_intent_phrases_from_text(message, state, message.text)


@dp.message(StateFilter("waiting_for_intent_add_phrases"), F.document)
async def handle_intent_add_phrases_document(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	try:
		text = await read_prompt_document(message.document)
	except PromptTextError as exc:
		await message.answer(f"Фразы не добавлены: {html.escape(str(exc))}", reply_markup=cancel_action_keyboard())
		return
	await _save_intent_phrases_from_text(message, state, text)


@dp.callback_query(F.data.startswith("response_intent_phrases:"))
async def cb_response_intent_phrases(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	try:
		intent_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	intent = get_trigger_intent(intent_id)
	if not intent:
		await callback.answer("Намерение не найдено.", show_alert=True)
		return
	await callback.message.edit_text(_intent_phrases_text(intent), reply_markup=_intent_phrases_keyboard(intent))
	await callback.answer()


@dp.callback_query(F.data.startswith("response_intent_phrase_delete_confirm:"))
async def cb_response_intent_phrase_delete_confirm(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		_, phrase_id, intent_id = callback.data.split(":", 2)
		phrase_id = int(phrase_id)
		intent_id = int(intent_id)
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	phrase = get_intent_phrase(phrase_id)
	if not phrase:
		await callback.answer("Фраза не найдена.", show_alert=True)
		return
	keyboard = InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Да, удалить", callback_data=f"response_intent_phrase_delete:{phrase_id}:{intent_id}")],
		[InlineKeyboardButton(text="Отмена", callback_data=f"response_intent_phrases:{intent_id}")],
	])
	await callback.message.edit_text(
		f"Удалить фразу?\n\n<code>{html.escape(phrase.get('phrase') or '')}</code>",
		reply_markup=keyboard,
	)
	await callback.answer()


@dp.callback_query(F.data.startswith("response_intent_phrase_delete:"))
async def cb_response_intent_phrase_delete(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		_, phrase_id, intent_id = callback.data.split(":", 2)
		phrase_id = int(phrase_id)
		intent_id = int(intent_id)
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	delete_intent_phrase(phrase_id)
	intent = get_trigger_intent(intent_id)
	if not intent:
		await callback.answer("Фраза удалена.")
		await cb_response_intents(callback, state)
		return
	await callback.message.edit_text(_intent_phrases_text(intent), reply_markup=_intent_phrases_keyboard(intent))
	await callback.answer("Фраза удалена.")


@dp.callback_query(F.data.startswith("response_intent_delete_confirm:"))
async def cb_response_intent_delete_confirm(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		intent_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	intent = get_trigger_intent(intent_id, include_phrases=False)
	if not intent:
		await callback.answer("Намерение не найдено.", show_alert=True)
		return
	keyboard = InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Да, удалить", callback_data=f"response_intent_delete:{intent_id}")],
		[InlineKeyboardButton(text="Отмена", callback_data=f"response_intent_view:{intent_id}")],
	])
	await callback.message.edit_text(
		f"Удалить намерение <b>{html.escape(intent.get('name') or '-')}</b>?\n\n"
		"Все его фразы тоже будут удалены.",
		reply_markup=keyboard,
	)
	await callback.answer()


@dp.callback_query(F.data.startswith("response_intent_delete:"))
async def cb_response_intent_delete(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		intent_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	deleted = delete_trigger_intent(intent_id)
	await callback.answer("Удалено." if deleted else "Не найдено.")
	await cb_response_intents(callback, state)


@dp.callback_query(F.data == "response_intents_defaults")
async def cb_response_intents_defaults(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	count = ensure_default_vpn_intents()
	await callback.answer(f"Готово: {count}")
	await callback.message.edit_text(_intents_text(), reply_markup=_intents_keyboard())


@dp.callback_query(F.data == "response_intent_add_start")
async def cb_response_intent_add_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	await callback.message.edit_text("Введите название намерения:", reply_markup=cancel_action_keyboard())
	await state.set_state("waiting_for_intent_name")
	await callback.answer()


@dp.message(StateFilter("waiting_for_intent_name"), F.text, ~F.text.startswith('/'))
async def handle_intent_name(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	name = message.text.strip()
	if not name:
		await message.answer("Название не может быть пустым.", reply_markup=cancel_action_keyboard())
		return
	await state.update_data(intent_name=name)
	await message.answer("Введите описание смысла намерения:", reply_markup=cancel_action_keyboard())
	await state.set_state("waiting_for_intent_description")


@dp.message(StateFilter("waiting_for_intent_description"), F.text, ~F.text.startswith('/'))
async def handle_intent_description(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	await state.update_data(intent_description=message.text.strip())
	await message.answer(
		"Введите ключевые фразы для этого намерения, каждую с новой строки.\n"
		"Если нужны только смысловые AI-срабатывания, отправьте <code>-</code>.",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state("waiting_for_intent_phrases")


@dp.message(StateFilter("waiting_for_intent_phrases"), F.text, ~F.text.startswith('/'))
async def handle_intent_phrases(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	data = await state.get_data()
	name = data.get("intent_name")
	description = data.get("intent_description", "")
	intent_id = upsert_trigger_intent(name, description, "openai", "", 1, 1)
	phrases = []
	if message.text.strip() != "-":
		phrases = [line.strip() for line in message.text.splitlines() if line.strip()]
	added = add_intent_phrases(intent_id, phrases) if phrases else 0
	await state.clear()
	await message.answer(
		f"Намерение сохранено.\n"
		f"Название: <b>{html.escape(name)}</b>\n"
		f"Фраз добавлено: <b>{added}</b>",
		reply_markup=_intents_keyboard()
	)


@dp.callback_query(F.data == "response_ai_settings")
async def cb_response_ai_settings(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	await callback.message.edit_text(
		_trigger_ai_settings_text(),
		reply_markup=_trigger_ai_settings_keyboard()
	)
	await callback.answer()


@dp.callback_query(F.data == "response_ai_prompt")
async def cb_response_ai_prompt(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	current_prompt = _get_trigger_ai_prompt()
	await callback.message.edit_text(
		"<b>Промпт AI для триггеров</b>\n"
		f"Текущая длина: <b>{len(current_prompt)}</b> симв.\n"
		"Текущий промпт отправлен .txt файлом ниже.\n\n"
		"Отправьте новый промпт сообщением или .txt файлом.",
		reply_markup=cancel_action_keyboard()
	)
	await send_prompt_preview_file(
		callback.message,
		current_prompt,
		"ai_trigger_prompt.txt",
		"Текущий промпт AI для триггеров",
	)
	await state.set_state("waiting_for_trigger_ai_prompt")
	await callback.answer()


@dp.callback_query(F.data == "response_ai_model")
async def cb_response_ai_model(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await callback.message.edit_text(
		f"Текущая модель для триггеров: <code>{html.escape(_get_trigger_ai_model())}</code>\n"
		"Введите новую модель, например <code>gpt-4o-mini</code>.",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state("waiting_for_trigger_ai_model")
	await callback.answer()


@dp.callback_query(F.data == "response_ai_temperature")
async def cb_response_ai_temperature(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await callback.message.edit_text(
		f"Текущая температура для триггеров: <code>{html.escape(_get_trigger_ai_temperature())}</code>\n"
		"Введите число от 0 до 2. Для живых ответов обычно норм: 0.7-1.0.",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state("waiting_for_trigger_ai_temperature")
	await callback.answer()


@dp.callback_query(F.data == "response_ai_max_tokens")
async def cb_response_ai_max_tokens(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await callback.message.edit_text(
		f"Текущий лимит токенов для триггеров: <code>{html.escape(_get_trigger_ai_max_tokens())}</code>\n"
		"Введите новое число. Для коротких ответов обычно хватает 80-150.",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state("waiting_for_trigger_ai_max_tokens")
	await callback.answer()


@dp.callback_query(F.data == "response_semantic_toggle")
async def cb_response_semantic_toggle(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	set_trigger_semantic_enabled(not semantic_analysis_enabled())
	await state.clear()
	await callback.message.edit_text(
		_trigger_ai_settings_text(),
		reply_markup=_trigger_ai_settings_keyboard()
	)
	await callback.answer("Настройка обновлена.")


@dp.callback_query(F.data == "response_semantic_threshold")
async def cb_response_semantic_threshold(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await callback.message.edit_text(
		f"Текущий порог уверенности: <code>{html.escape(_get_semantic_threshold())}</code>\n"
		"Введите число от 0 до 1. Обычно норм: 0.70-0.80.",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state("waiting_for_semantic_threshold")
	await callback.answer()


@dp.callback_query(F.data == "response_semantic_model")
async def cb_response_semantic_model(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await callback.message.edit_text(
		f"Текущая модель классификации: <code>{html.escape(_get_semantic_model())}</code>\n"
		"Введите новую модель, например <code>gpt-4o-mini</code>.",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state("waiting_for_semantic_model")
	await callback.answer()


@dp.callback_query(F.data == "response_semantic_rate")
async def cb_response_semantic_rate(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await callback.message.edit_text(
		f"Текущий лимит AI-проверок смысла в минуту: <code>{html.escape(_get_semantic_rate_limit())}</code>\n"
		"Введите целое число. 0 полностью блокирует AI-классификацию.",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state("waiting_for_semantic_rate")
	await callback.answer()


@dp.callback_query(F.data == "response_cooldowns")
async def cb_response_cooldowns(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await callback.message.edit_text(
		f"Текущий cooldown чата: <code>{html.escape(_get_chat_cooldown())}</code> сек.\n"
		f"Текущий cooldown пользователя: <code>{html.escape(_get_user_cooldown())}</code> сек.\n\n"
		"Введите два числа через пробел: <code>120 300</code>.",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state("waiting_for_trigger_cooldowns")
	await callback.answer()


@dp.callback_query(F.data == "response_all_ai_confirm")
async def cb_response_all_ai_confirm(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	keyboard = InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Да, включить AI для всех", callback_data="response_all_ai")],
		[InlineKeyboardButton(text="Отмена", callback_data="response_list")]
	])
	await callback.message.edit_text(
		"Перевести все триггеры в AI-режим?\n\n"
		"Готовые тексты у этих триггеров будут очищены.",
		reply_markup=keyboard
	)
	await callback.answer()


@dp.message(StateFilter("waiting_for_trigger_ai_prompt"), F.text, ~F.text.startswith('/'))
async def handle_trigger_ai_prompt_text(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	prompt = message.text.strip()
	if not prompt:
		await message.answer("Промпт не может быть пустым.", reply_markup=cancel_action_keyboard())
		return
	set_config_value("ai_trigger_prompt", prompt)
	await state.clear()
	await message.answer(
		f"Промпт AI для триггеров обновлен. Длина: {len(prompt)} симв.",
		reply_markup=_trigger_ai_settings_keyboard()
	)


@dp.message(StateFilter("waiting_for_trigger_ai_prompt"), F.document)
async def handle_trigger_ai_prompt_document(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	try:
		prompt = await read_prompt_document(message.document)
	except PromptTextError as exc:
		await message.answer(
			f"Промпт не обновлен: {html.escape(str(exc))}",
			reply_markup=cancel_action_keyboard()
		)
		return
	set_config_value("ai_trigger_prompt", prompt)
	await state.clear()
	await message.answer(
		f"Промпт AI для триггеров обновлен из .txt. Длина: {len(prompt)} симв.",
		reply_markup=_trigger_ai_settings_keyboard()
	)


@dp.message(StateFilter("waiting_for_trigger_ai_model"), F.text, ~F.text.startswith('/'))
async def handle_trigger_ai_model(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	model = message.text.strip()
	if not model:
		await message.answer("Модель не может быть пустой.", reply_markup=cancel_action_keyboard())
		return
	set_config_value("ai_trigger_model", model)
	await state.clear()
	await message.answer("Модель AI для триггеров обновлена.", reply_markup=_trigger_ai_settings_keyboard())


@dp.message(StateFilter("waiting_for_trigger_ai_temperature"), F.text, ~F.text.startswith('/'))
async def handle_trigger_ai_temperature(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	try:
		value = float(message.text.strip().replace(",", "."))
	except ValueError:
		await message.answer("Введите число, например 0.8.", reply_markup=cancel_action_keyboard())
		return
	if not 0 <= value <= 2:
		await message.answer("Температура должна быть от 0 до 2.", reply_markup=cancel_action_keyboard())
		return
	set_config_value("ai_trigger_temperature", str(value))
	await state.clear()
	await message.answer("Температура AI для триггеров обновлена.", reply_markup=_trigger_ai_settings_keyboard())


@dp.message(StateFilter("waiting_for_trigger_ai_max_tokens"), F.text, ~F.text.startswith('/'))
async def handle_trigger_ai_max_tokens(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	try:
		value = int(message.text.strip())
	except ValueError:
		await message.answer("Введите целое число, например 120.", reply_markup=cancel_action_keyboard())
		return
	if not 1 <= value <= 4000:
		await message.answer("Лимит должен быть от 1 до 4000.", reply_markup=cancel_action_keyboard())
		return
	set_config_value("ai_trigger_max_tokens", str(value))
	await state.clear()
	await message.answer("Лимит токенов AI для триггеров обновлен.", reply_markup=_trigger_ai_settings_keyboard())


@dp.message(StateFilter("waiting_for_semantic_threshold"), F.text, ~F.text.startswith('/'))
async def handle_semantic_threshold(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	try:
		value = float(message.text.strip().replace(",", "."))
	except ValueError:
		await message.answer("Введите число, например 0.72.", reply_markup=cancel_action_keyboard())
		return
	if not 0 <= value <= 1:
		await message.answer("Порог должен быть от 0 до 1.", reply_markup=cancel_action_keyboard())
		return
	set_config_value("trigger_semantic_threshold", str(value))
	await state.clear()
	await message.answer("Порог AI-смысла обновлен.", reply_markup=_trigger_ai_settings_keyboard())


@dp.message(StateFilter("waiting_for_semantic_model"), F.text, ~F.text.startswith('/'))
async def handle_semantic_model(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	model = message.text.strip()
	if not model:
		await message.answer("Модель не может быть пустой.", reply_markup=cancel_action_keyboard())
		return
	set_config_value("trigger_semantic_model", model)
	await state.clear()
	await message.answer("Модель классификации обновлена.", reply_markup=_trigger_ai_settings_keyboard())


@dp.message(StateFilter("waiting_for_semantic_rate"), F.text, ~F.text.startswith('/'))
async def handle_semantic_rate(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	try:
		value = int(message.text.strip())
	except ValueError:
		await message.answer("Введите целое число, например 30.", reply_markup=cancel_action_keyboard())
		return
	if value < 0:
		await message.answer("Лимит не может быть отрицательным.", reply_markup=cancel_action_keyboard())
		return
	set_config_value("trigger_semantic_max_per_minute", str(value))
	await state.clear()
	await message.answer("Лимит AI-проверок обновлен.", reply_markup=_trigger_ai_settings_keyboard())


@dp.message(StateFilter("waiting_for_trigger_cooldowns"), F.text, ~F.text.startswith('/'))
async def handle_trigger_cooldowns(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	parts = message.text.replace(",", " ").split()
	if len(parts) != 2:
		await message.answer("Введите два числа через пробел, например: 120 300", reply_markup=cancel_action_keyboard())
		return
	try:
		chat_cd, user_cd = int(parts[0]), int(parts[1])
	except ValueError:
		await message.answer("Оба значения должны быть целыми числами.", reply_markup=cancel_action_keyboard())
		return
	if chat_cd < 0 or user_cd < 0:
		await message.answer("Cooldown не может быть отрицательным.", reply_markup=cancel_action_keyboard())
		return
	set_config_value("trigger_chat_cooldown_seconds", str(chat_cd))
	set_config_value("trigger_user_cooldown_seconds", str(user_cd))
	await state.clear()
	await message.answer("Cooldown-ы обновлены.", reply_markup=_trigger_ai_settings_keyboard())


@dp.callback_query(F.data == "response_all_ai")
async def cb_response_all_ai(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	updated = _set_all_responses_ai()
	await callback.answer(f"Обновлено: {updated}")
	await cb_response_list(callback, state)


@dp.callback_query(F.data == "response_delete_all_confirm")
async def cb_response_delete_all_confirm(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	keyboard = InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Да, удалить все", callback_data="response_delete_all")],
		[InlineKeyboardButton(text="Отмена", callback_data="response_list")]
	])
	await callback.message.edit_text(
		"Удалить все триггеры?\n\n"
		"Это действие нельзя отменить.",
		reply_markup=keyboard
	)
	await callback.answer()


@dp.callback_query(F.data == "response_delete_all")
async def cb_response_delete_all(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	deleted = _delete_all_responses()
	await callback.answer(f"Удалено: {deleted}")
	await cb_response_list(callback, state)


@dp.callback_query(F.data.startswith("response_view:"))
async def cb_response_view(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await state.clear()
	try:
		response_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	item = _get_response_by_id(response_id)
	if not item:
		await callback.answer("Триггер не найден.", show_alert=True)
		await cb_response_list(callback, state)
		return
	await callback.message.edit_text(_format_response_detail(item), reply_markup=_response_detail_keyboard(response_id))
	await callback.answer()


@dp.message(StateFilter("waiting_for_keyword"), F.text, ~F.text.startswith('/'))
async def handle_keyword_input(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	keyword = message.text.strip().lower()
	if not keyword:
		await message.answer("Ключевое слово не может быть пустым. Попробуйте снова.")
		return
	if len(keyword) > 80:
		await message.answer("Слишком длинный триггер. Сделай до 80 символов.")
		return
	await state.update_data(keyword=keyword)
	keyboard = InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Готовый текст", callback_data="choice_predefined")],
		[InlineKeyboardButton(text="AI-генерация", callback_data="choice_openai")],
		[InlineKeyboardButton(text="Отмена", callback_data="set_answer")]
	])
	await message.answer(f"Триггер: <b>{html.escape(keyword)}</b>\nВыбери тип ответа:", reply_markup=keyboard)


@dp.callback_query(F.data == "choice_predefined")
async def cb_choice_predefined(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await callback.message.edit_text("Введите текст ответа:", reply_markup=cancel_action_keyboard())
	await state.set_state("waiting_for_predefined_text")
	await callback.answer()


@dp.message(StateFilter("waiting_for_predefined_text"), F.text, ~F.text.startswith('/'))
async def handle_predefined_text(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("У вас нет прав.")
		return
	data = await state.get_data()
	keyword = data.get("keyword")
	if not keyword:
		await message.answer("Ключевое слово не найдено. Начните заново /start", reply_markup=main_menu_keyboard())
		await state.clear()
		return

	predefined_answer = message.text.strip()
	if not predefined_answer:
		await message.answer("Ответ не может быть пустым. Введите текст.")
		return

	_save_response(keyword, predefined_answer, "predefined")
	await state.clear()
	await message.answer(
		f"Триггер сохранен.\n\n"
		f"<b>Ключ:</b> <code>{html.escape(keyword)}</code>\n"
		f"<b>Ответ:</b>\n<pre>{html.escape(predefined_answer)}</pre>",
		reply_markup=responses_menu_keyboard()
	)


@dp.callback_query(F.data == "choice_openai")
async def cb_choice_openai(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	data = await state.get_data()
	keyword = data.get("keyword")
	if not keyword:
		await callback.message.edit_text(
			"Ключевое слово не найдено. Начните заново /start",
			reply_markup=main_menu_keyboard()
		)
		await state.clear()
		return

	_save_response(keyword, "", "openai")
	await state.clear()
	await callback.message.edit_text(
		f"Триггер сохранен.\n\n"
		f"<b>Ключ:</b> <code>{html.escape(keyword)}</code>\n"
		"При обнаружении будет генерироваться AI-ответ.",
		reply_markup=responses_menu_keyboard()
	)
	await callback.answer()


@dp.callback_query(F.data.startswith("response_edit_text:"))
async def cb_response_edit_text(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		response_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	item = _get_response_by_id(response_id)
	if not item:
		await callback.answer("Триггер не найден.", show_alert=True)
		return
	await state.update_data(keyword=item["keyword"], response_id=response_id)
	await callback.message.edit_text(
		f"Введите новый текст для триггера <code>{html.escape(item['keyword'])}</code>:",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state("waiting_for_predefined_text")
	await callback.answer()


@dp.callback_query(F.data.startswith("response_set_ai:"))
async def cb_response_set_ai(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		response_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	item = _get_response_by_id(response_id)
	if not item:
		await callback.answer("Триггер не найден.", show_alert=True)
		return
	_save_response(item["keyword"], "", "openai")
	updated = _get_response_by_id(response_id) or item
	await callback.message.edit_text(_format_response_detail(updated), reply_markup=_response_detail_keyboard(response_id))
	await callback.answer("Переключено в AI-режим.")


@dp.callback_query(F.data.startswith("response_delete_confirm:"))
async def cb_response_delete_confirm(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		response_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	item = _get_response_by_id(response_id)
	if not item:
		await callback.answer("Триггер не найден.", show_alert=True)
		return
	keyboard = InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Да, удалить", callback_data=f"response_delete:{response_id}")],
		[InlineKeyboardButton(text="Отмена", callback_data=f"response_view:{response_id}")]
	])
	await callback.message.edit_text(
		f"Удалить триггер <code>{html.escape(item['keyword'])}</code>?",
		reply_markup=keyboard
	)
	await callback.answer()


@dp.callback_query(F.data.startswith("response_delete:"))
async def cb_response_delete(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	try:
		response_id = int(callback.data.split(":", 1)[1])
	except (ValueError, IndexError):
		await callback.answer("Некорректный ID.", show_alert=True)
		return
	deleted = _delete_response(response_id)
	await callback.answer("Удалено." if deleted else "Не найдено.")
	await cb_response_list(callback, state)
