# C:\Users\27030\Desktop\TEMP\admin_bot\handlers\responses.py
from aiogram import F
from aiogram.filters import StateFilter
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from ..bot_instance import dp
from ..utils import user_is_allowed
from ..keyboards import main_menu_keyboard
from db import get_db_connection


@dp.callback_query(F.data == "set_answer")
async def cb_set_answer(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await callback.message.edit_text(
		"🔑 Введите ключевое слово (например, привет, заказ, помощь, впн). Регистр не важен.")
	await state.set_state("waiting_for_keyword")
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
	await state.update_data(keyword=keyword)
	kb = InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="💬 Предустановленная фраза", callback_data="choice_predefined")],
		[InlineKeyboardButton(text="🤖 OpenAI/G4F генерация", callback_data="choice_openai")],
		[InlineKeyboardButton(text="⬅️ Отмена (в главное меню)", callback_data="back_to_main_menu")]
	])
	await message.answer(f"Ключевое слово: <b>{keyword}</b>. Выберите тип ответа:", reply_markup=kb)


@dp.callback_query(F.data == "choice_predefined")
async def cb_choice_predefined(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	await callback.message.edit_text("Введите предустановленный ответ:")
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
	predefined_answer = message.text

	conn = get_db_connection()
	cursor = conn.cursor()
	cursor.execute(
		"INSERT OR REPLACE INTO responses (keyword, answer, response_type) VALUES (?, ?, ?)",
		(keyword, predefined_answer, "predefined")
	)
	conn.commit()
	conn.close()

	await message.answer(f"Ключевое слово: {keyword}\nОтвет:\n<pre>{predefined_answer}</pre>\nУспешно сохранено.",
						 reply_markup=main_menu_keyboard())
	await state.clear()


@dp.callback_query(F.data == "choice_openai")
async def cb_choice_openai(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("У вас нет прав.")
		return
	data = await state.get_data()
	keyword = data.get("keyword")
	if not keyword:
		await callback.message.edit_text("Ключевое слово не найдено. Начните заново /start",
										 reply_markup=main_menu_keyboard())
		await state.clear()
		return

	conn = get_db_connection()
	cursor = conn.cursor()
	cursor.execute(
		"INSERT OR REPLACE INTO responses (keyword, answer, response_type) VALUES (?, ?, ?)",
		(keyword, "", "openai")
	)
	conn.commit()
	conn.close()

	await callback.message.edit_text(
		f"Ключевое слово: {keyword}\nПри обнаружении будет генерироваться ответ (OpenAI/G4F).",
		reply_markup=main_menu_keyboard()
	)
	await callback.answer()
	await state.clear()
