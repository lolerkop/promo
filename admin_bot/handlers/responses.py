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

from db import get_config_value, get_db_connection

from ..bot_instance import dp
from ..keyboards import cancel_action_keyboard, main_menu_keyboard
from ..utils import user_is_allowed


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
