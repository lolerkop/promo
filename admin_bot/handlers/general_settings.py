# C:\Users\27030\Desktop\TEMP\admin_bot\handlers\general_settings.py
import logging
import re
from typing import cast

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from admin_bot.states import TemplatesState
from db import get_config_value, get_templates, set_config_value, set_template
from userbot import init_listen_all

from ..bot_instance import dp
from ..utils import user_is_allowed

TEMPLATE_PATTERN = re.compile(r'^(?:\d+\[[^\]\n]+\](?:\n|\s)?)+$')

def general_settings_menu_keyboard() -> InlineKeyboardMarkup:
	subscription_mode = get_config_value("subscription_mode", "all_accounts")
	sub_mode_text = "Все аккаунты" if subscription_mode == "all_accounts" else "1 сущность - 1 аккаунт"

	listen_all_mode = get_config_value("listen_all", "True").lower() == 'true'
	listen_all_text = "Все чаты" if listen_all_mode else "Только из БД"

	buttons = [
		[InlineKeyboardButton(text=f"Режим подписки: {sub_mode_text}", callback_data="toggle_subscription_mode")],
		[InlineKeyboardButton(text=f"Режим отслеживания: {listen_all_text}", callback_data="toggle_listen_all")],
		[InlineKeyboardButton(text="Шаблоны", callback_data="view_templates")],
		[InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
	]
	return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "general_settings_menu")
async def cb_general_settings_menu(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	await callback.message.edit_text(
		"🛠️ <b>Общие настройки</b>\nЗдесь вы можете настроить общие параметры бота.",
		reply_markup=general_settings_menu_keyboard()
	)
	await callback.answer()

@dp.callback_query(F.data == "view_templates")
async def view_templates(callback: CallbackQuery):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return

	buttons = [
		[InlineKeyboardButton(text="Добавить шаблон", callback_data="add_template")],
		[InlineKeyboardButton(text="Очистить шаблоны", callback_data="clear_templates")]
	]

	keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
	message = cast(Message, callback.message)

	templates = get_templates()
	if templates:
		templates = re.compile(r'(\d+\[[^\]\n]+\])').findall(templates)
		templates_text = '\n'.join(f"{' ' * 6}{t}" for t in templates)
	else:
		templates_text = f"{' ' * 6}Пусто..."

	await message.edit_text(f"🛠️ Шаблоны:\n{templates_text}", reply_markup=keyboard)

@dp.callback_query(F.data == "add_template")
async def add_template_callback(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return

	await callback.answer()
	await state.clear()

	back_button = [InlineKeyboardButton(text="⬅️ Отменить", callback_data="back_templates")]
	keyboard = InlineKeyboardMarkup(inline_keyboard=[back_button])

	await callback.message.answer("Будь-те осторожны, ваши текущие шаблоны будут удалены, и заменены")
	
	await callback.message.answer("Отправьте строку с шаблонами\n\nПример: 1[пример]", reply_markup=keyboard)
	await state.set_state(TemplatesState.new_template)

@dp.message(TemplatesState.new_template)
async def add_template_message(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("Нет прав.")
		return

	if not TEMPLATE_PATTERN.match(str(message.text)) or not message.text:
		await message.answer("Не валидный формат шаблона!")
		return

	set_template(message.text)
	await message.answer("✅ Новый шаблон успешно добавлен")
	logging.info("The list of templates has been updated")
	await state.clear()

@dp.callback_query(F.data == "back_templates")
async def back_template(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	
	await callback.answer()
	await state.clear()
	message = cast(Message, callback.message)
	await message.edit_text(
		"🛠️ <b>Общие настройки</b>\nЗдесь вы можете настроить общие параметры бота.",
		reply_markup=general_settings_menu_keyboard()
	)

	await callback.answer()

@dp.callback_query(F.data == "clear_templates")
async def clear_templates(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	
	await callback.answer()
	await state.clear()
	set_template("")
	logging.info("The list of templates has been cleared")

	message = cast(Message, callback.message)

	await message.edit_text("Список шаблонов успешно очищено")

@dp.callback_query(F.data == "toggle_subscription_mode")
async def cb_toggle_subscription_mode(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return

	current_mode = get_config_value("subscription_mode", "all_accounts")
	new_mode = "single_account_sticky" if current_mode == "all_accounts" else "all_accounts"
	set_config_value("subscription_mode", new_mode)

	logging.info(f"Subscription mode changed to: {new_mode} by admin {callback.from_user.id}")
	await callback.message.edit_text(
		"Настройки обновлены.",
		reply_markup=general_settings_menu_keyboard()
	)
	await callback.answer("Режим подписки изменен.")


@dp.callback_query(F.data == "toggle_listen_all")
async def cb_toggle_listen_all(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return

	current_status = get_config_value("listen_all", "True").lower() == 'true'
	new_status_str = "False" if current_status else "True"
	set_config_value("listen_all", new_status_str)

	init_listen_all(new_status_str.lower() == 'true')

	logging.info(f"Listen_all mode changed to: {new_status_str} by admin {callback.from_user.id}")
	await callback.message.edit_text(
		"Настройки обновлены.",
		reply_markup=general_settings_menu_keyboard()
	)
	await callback.answer("Режим отслеживания изменен.")
