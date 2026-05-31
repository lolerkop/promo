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


def _trigger_ai_settings_text() -> str:
	prompt = _get_trigger_ai_prompt()
	return (
		"<b>AI-настройки триггеров</b>\n\n"
		f"Модель: <code>{html.escape(_get_trigger_ai_model())}</code>\n"
		f"Температура: <b>{html.escape(_get_trigger_ai_temperature())}</b>\n"
		f"Макс. токены: <b>{html.escape(_get_trigger_ai_max_tokens())}</b>\n"
		f"Промпт: <b>{len(prompt)}</b> симв.\n\n"
		"Эти настройки используются только для ответов на триггеры. "
		"Обычные автокомментарии и диалоги остаются на своих AI-настройках."
	)


def _trigger_ai_settings_keyboard() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Промпт триггеров", callback_data="response_ai_prompt")],
		[InlineKeyboardButton(text=f"Модель: {_get_trigger_ai_model()}", callback_data="response_ai_model")],
		[
			InlineKeyboardButton(text=f"Температура: {_get_trigger_ai_temperature()}", callback_data="response_ai_temperature"),
			InlineKeyboardButton(text=f"Токены: {_get_trigger_ai_max_tokens()}", callback_data="response_ai_max_tokens"),
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


def responses_menu_keyboard() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Добавить / обновить триггер", callback_data="response_add_start")],
		[InlineKeyboardButton(text="Список и общие настройки", callback_data="response_list")],
		[InlineKeyboardButton(text="AI-настройки триггеров", callback_data="response_ai_settings")],
		[InlineKeyboardButton(text="Назад в главное меню", callback_data="back_to_main_menu")]
	])


def _responses_menu_text() -> str:
	responses = _fetch_responses()
	total, ai_count, text_count = _response_stats(responses)
	listen_all = get_config_value("listen_all", "True").lower() == "true"
	tracking_text = "все чаты" if listen_all else "только включенные группы/категории из БД"
	return (
		"<b>Ответы на триггеры</b>\n\n"
		f"Всего триггеров: <b>{total}</b>\n"
		f"AI-ответов: <b>{ai_count}</b>\n"
		f"Готовых текстов: <b>{text_count}</b>\n"
		f"Режим отслеживания: <b>{tracking_text}</b>\n\n"
		"Когда аккаунт видит сообщение с ключевой фразой, он отвечает реплаем в этот чат."
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
