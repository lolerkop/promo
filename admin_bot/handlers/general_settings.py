# C:\Users\27030\Desktop\TEMP\admin_bot\handlers\general_settings.py
import logging
import re
from typing import cast

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from admin_bot.states import GeneralSettingsStates, TemplatesState
from db import (create_workspace, delete_workspace, get_active_workspace_id,
                get_active_workspace_name, get_config_value, get_templates,
                list_workspaces, set_config_value, set_template,
                set_workspace_running,
                switch_workspace)
from userbot import (init_listen_all, restart_all_clients,
                     start_workspace_clients, stop_workspace_clients)
from shared import active_background_tasks

from ..bot_instance import dp
from ..utils import user_is_allowed

TEMPLATE_PATTERN = re.compile(r'^(?:\d+\[[^\]\n]+\](?:\n|\s)?)+$')


async def _cancel_workspace_background_tasks(workspace_id: str = None):
	for task_id, task_info in list(active_background_tasks.items()):
		if workspace_id and task_info.get("workspace_id") != workspace_id:
			continue
		task = task_info.get("task")
		if task and not task.done():
			task.cancel()
		active_background_tasks.pop(task_id, None)

def general_settings_menu_keyboard() -> InlineKeyboardMarkup:
	subscription_mode = get_config_value("subscription_mode", "all_accounts")
	subscription_mode_names = {
		"all_accounts": "Все аккаунты",
		"single_account_sticky": "1 сущность - 1 аккаунт",
		"one_entity_five_accounts": "1 на 5",
	}
	sub_mode_text = subscription_mode_names.get(subscription_mode, subscription_mode_names["all_accounts"])

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


def workspaces_menu_keyboard() -> InlineKeyboardMarkup:
	active_id = get_active_workspace_id()
	buttons = []
	for workspace in list_workspaces():
		workspace_id = workspace["id"]
		name = workspace.get("name") or workspace_id
		is_running = workspace.get("is_running", True)
		active_prefix = "[активно] " if workspace_id == active_id else ""
		runtime_prefix = "[запущено] " if is_running else "[остановлено] "
		buttons.append([
			InlineKeyboardButton(
				text=active_prefix + runtime_prefix + name,
				callback_data=f"workspace_switch:{workspace_id}"
			)
		])
		buttons.append([
			InlineKeyboardButton(
				text=("Остановить работу" if is_running else "Запустить работу") + f": {name}",
				callback_data=f"workspace_runtime_toggle:{workspace_id}"
			)
		])
		if workspace_id != "default":
			buttons.append([
				InlineKeyboardButton(text=f"Удалить: {name}", callback_data=f"workspace_delete_confirm:{workspace_id}")
			])
	buttons.append([InlineKeyboardButton(text="Добавить пространство", callback_data="workspace_add_start")])
	buttons.append([InlineKeyboardButton(text="Назад в общие настройки", callback_data="general_settings_menu")])
	return InlineKeyboardMarkup(inline_keyboard=buttons)


@dp.callback_query(F.data == "workspaces_menu")
async def cb_workspaces_menu(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	await callback.message.edit_text(
		f"🗄️ <b>Рабочие пространства</b>\nАктивно: <b>{get_active_workspace_name()}</b>\n\n"
		"Каждое пространство хранит свои аккаунты, каналы, группы, настройки, ключи и отчеты.",
		reply_markup=workspaces_menu_keyboard()
	)
	await callback.answer()


@dp.callback_query(F.data == "workspace_add_start")
async def cb_workspace_add_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await callback.message.edit_text(
		"Введите название нового рабочего пространства.",
		reply_markup=InlineKeyboardMarkup(inline_keyboard=[
			[InlineKeyboardButton(text="❌ Отмена", callback_data="workspaces_menu")]
		])
	)
	await state.set_state(GeneralSettingsStates.WaitingForWorkspaceName)
	await callback.answer()


@dp.message(GeneralSettingsStates.WaitingForWorkspaceName, F.text, ~F.text.startswith('/'))
async def process_workspace_name(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("Нет прав.")
		return
	name = message.text.strip()
	if len(name) < 2:
		await message.answer("Название слишком короткое. Введите минимум 2 символа.")
		return
	try:
		workspace = create_workspace(name)
		await start_workspace_clients(workspace["id"])
		await state.clear()
		await message.answer(
			f"✅ Пространство <b>{workspace['name']}</b> создано и выбрано.",
			reply_markup=workspaces_menu_keyboard()
		)
	except Exception as e:
		await message.answer(f"Не удалось создать пространство: {e}", reply_markup=workspaces_menu_keyboard())
		await state.clear()


@dp.callback_query(F.data.startswith("workspace_switch:"))
async def cb_workspace_switch(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	workspace_id = callback.data.split(":", 1)[1]
	if workspace_id == get_active_workspace_id():
		await callback.answer("Это пространство уже активно.")
		return
	try:
		await callback.message.edit_text("Переключаю интерфейс на выбранное пространство...")
		workspace = switch_workspace(workspace_id)
		await state.clear()
		await callback.message.edit_text(
			f"✅ Активное пространство: <b>{workspace['name']}</b>",
			reply_markup=workspaces_menu_keyboard()
		)
		await callback.answer("Переключено.")
	except Exception as e:
		await callback.message.edit_text(f"Не удалось переключить пространство: {e}", reply_markup=workspaces_menu_keyboard())
		await callback.answer("Ошибка.", show_alert=True)


@dp.callback_query(F.data.startswith("workspace_runtime_toggle:"))
async def cb_workspace_runtime_toggle(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	workspace_id = callback.data.split(":", 1)[1]
	workspace = next((item for item in list_workspaces() if item["id"] == workspace_id), None)
	if not workspace:
		await callback.answer("Пространство не найдено.", show_alert=True)
		return
	try:
		if workspace.get("is_running", True):
			await _cancel_workspace_background_tasks(workspace_id)
			await stop_workspace_clients(workspace_id)
			set_workspace_running(workspace_id, False)
			status_text = "остановлено"
		else:
			set_workspace_running(workspace_id, True)
			await start_workspace_clients(workspace_id)
			status_text = "запущено"
		await state.clear()
		await callback.message.edit_text(
			f"Работа пространства {status_text}: <b>{workspace.get('name') or workspace_id}</b>",
			reply_markup=workspaces_menu_keyboard()
		)
		await callback.answer(status_text)
	except Exception as e:
		await callback.message.edit_text(
			f"Не удалось изменить работу пространства: {e}",
			reply_markup=workspaces_menu_keyboard()
		)
		await callback.answer("Ошибка.", show_alert=True)


@dp.callback_query(F.data.startswith("workspace_delete_confirm:"))
async def cb_workspace_delete_confirm(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	workspace_id = callback.data.split(":", 1)[1]
	workspace = next((item for item in list_workspaces() if item["id"] == workspace_id), None)
	if not workspace or workspace_id == "default":
		await callback.answer("Это пространство нельзя удалить.", show_alert=True)
		return
	await callback.message.edit_text(
		f"Удалить пространство <b>{workspace['name']}</b>?\n"
		"Будут удалены его база, аккаунты, каналы, группы, настройки и сессии.",
		reply_markup=InlineKeyboardMarkup(inline_keyboard=[
			[InlineKeyboardButton(text="🔴 Да, удалить", callback_data=f"workspace_delete_execute:{workspace_id}")],
			[InlineKeyboardButton(text="⬅️ Назад", callback_data="workspaces_menu")]
		])
	)
	await callback.answer()


@dp.callback_query(F.data.startswith("workspace_delete_execute:"))
async def cb_workspace_delete_execute(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	workspace_id = callback.data.split(":", 1)[1]
	try:
		was_active = workspace_id == get_active_workspace_id()
		await _cancel_workspace_background_tasks(workspace_id)
		await stop_workspace_clients(workspace_id)
		delete_workspace(workspace_id)
		await state.clear()
		await callback.message.edit_text("✅ Пространство удалено.", reply_markup=workspaces_menu_keyboard())
		await callback.answer("Удалено.")
	except Exception as e:
		await callback.message.edit_text(f"Не удалось удалить пространство: {e}", reply_markup=workspaces_menu_keyboard())
		await callback.answer("Ошибка.", show_alert=True)


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
	mode_order = ["all_accounts", "single_account_sticky", "one_entity_five_accounts"]
	try:
		new_mode = mode_order[(mode_order.index(current_mode) + 1) % len(mode_order)]
	except ValueError:
		new_mode = "all_accounts"
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
