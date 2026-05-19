import html
import os
import tempfile
import time

from aiogram import F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from db import (clear_bot_errors, get_config_value, get_current_workspace_id,
                list_bot_errors, record_health_check_run, set_config_value,
                update_all_tables)
from services.account_health import (format_account_health_result,
                                     run_account_health_check)
from services.backup_service import (create_backup, get_latest_backup,
                                     human_size, list_backups)
from services.backup_service import restore_backup as restore_backup_file
from services.health_service import build_health_snapshot, format_health_dashboard
from services.proxy_health import (progress_bar, proxy_display_status,
                                   run_proxy_health_check)
from userbot import restart_workspace_clients, start_workspace_clients, stop_workspace_clients

from ..bot_instance import bot, dp
from ..keyboards import cancel_action_keyboard
from ..states import MaintenanceStates
from ..utils import user_is_allowed


def maintenance_menu_keyboard() -> InlineKeyboardMarkup:
	auto_enabled = get_config_value("maintenance_auto_checks_enabled", "1") == "1"
	return InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Обновить dashboard", callback_data="maintenance_menu")],
		[
			InlineKeyboardButton(text="Проверить прокси", callback_data="maintenance_check_proxies"),
			InlineKeyboardButton(text="Проверить аккаунты", callback_data="maintenance_check_accounts")
		],
		[InlineKeyboardButton(
			text=("Выключить автопроверки" if auto_enabled else "Включить автопроверки"),
			callback_data="maintenance_toggle_auto"
		)],
		[
			InlineKeyboardButton(text="Backups", callback_data="maintenance_backups"),
			InlineKeyboardButton(text="Журнал ошибок", callback_data="maintenance_errors")
		],
		[InlineKeyboardButton(text="Назад в главное меню", callback_data="back_to_main_menu")]
	])


def backup_menu_keyboard() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Создать backup БД/отчетов", callback_data="maintenance_backup_create_db")],
		[InlineKeyboardButton(text="Создать backup с сессиями", callback_data="maintenance_backup_create_sessions")],
		[InlineKeyboardButton(text="Скачать последний backup", callback_data="maintenance_backup_download_latest")],
		[InlineKeyboardButton(text="Восстановить из backup", callback_data="maintenance_backup_restore_start")],
		[InlineKeyboardButton(text="Назад", callback_data="maintenance_menu")]
	])


def errors_menu_keyboard() -> InlineKeyboardMarkup:
	return InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Обновить", callback_data="maintenance_errors")],
		[InlineKeyboardButton(text="Очистить журнал", callback_data="maintenance_errors_clear_confirm")],
		[InlineKeyboardButton(text="Назад", callback_data="maintenance_menu")]
	])


async def _show_maintenance_menu(target, state: FSMContext = None):
	if state:
		await state.clear()
	text = format_health_dashboard(build_health_snapshot())
	if isinstance(target, CallbackQuery):
		await target.message.edit_text(text, reply_markup=maintenance_menu_keyboard())
		await target.answer()
	else:
		await target.answer(text, reply_markup=maintenance_menu_keyboard())


@dp.callback_query(F.data == "maintenance_menu")
async def cb_maintenance_menu(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await _show_maintenance_menu(callback, state)


@dp.message(Command("maintenance"))
async def cmd_maintenance(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("Нет прав.")
		return
	await _show_maintenance_menu(message, state)


@dp.callback_query(F.data == "maintenance_toggle_auto")
async def cb_maintenance_toggle_auto(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	current = get_config_value("maintenance_auto_checks_enabled", "1")
	new_value = "0" if current == "1" else "1"
	set_config_value("maintenance_auto_checks_enabled", new_value)
	await callback.answer("Автопроверки обновлены.")
	await _show_maintenance_menu(callback, state)


@dp.callback_query(F.data == "maintenance_check_proxies")
async def cb_maintenance_check_proxies(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	last_edit_at = 0.0
	started_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())

	async def progress(progress_data: dict):
		nonlocal last_edit_at
		now = time.monotonic()
		completed = progress_data.get("completed", 0)
		total = progress_data.get("total", 0)
		if completed != total and now - last_edit_at < 1.5:
			return
		last_edit_at = now
		stats = progress_data.get("stats", {})
		tail = []
		for item in progress_data.get("result_lines", [])[-8:]:
			tail.append(
				f"#{item['id']} <code>{html.escape(item['addr'])}</code> - "
				f"<b>{html.escape(proxy_display_status(item['status']))}</b>: "
				f"<code>{html.escape(str(item['text'])[:140])}</code>"
			)
		await callback.message.edit_text(
			"<b>Проверяю прокси</b>\n\n"
			f"{progress_bar(completed, total)} <b>{completed}/{total}</b>\n"
			f"Рабочие: <b>{stats.get('active', 0)}</b>\n"
			f"Не работают: <b>{stats.get('failed', 0)}</b>\n"
			f"Истекли: <b>{stats.get('expired', 0)}</b>\n\n"
			f"{chr(10).join(tail) if tail else 'Жду первые результаты...'}"
		)

	await callback.answer("Начинаю проверку.")
	result = await run_proxy_health_check(concurrency=5, progress_callback=progress)
	if result.get("affected_accounts"):
		await callback.message.edit_text("Плохие прокси найдены. Перезапускаю клиентов пространства...")
		await restart_workspace_clients()
	record_health_check_run(
		"proxy_manual",
		started_at,
		"ok",
		f"active={result['stats']['active']}, failed={result['stats']['failed']}, expired={result['stats']['expired']}"
	)
	await callback.message.edit_text(
		"<b>Проверка прокси завершена</b>\n\n"
		f"Всего: <b>{result['total']}</b>\n"
		f"Рабочие: <b>{result['stats']['active']}</b>\n"
		f"Не работают: <b>{result['stats']['failed']}</b>\n"
		f"Истекли: <b>{result['stats']['expired']}</b>\n"
		f"Аккаунтов затронуто: <b>{len(result.get('affected_accounts', []))}</b>",
		reply_markup=maintenance_menu_keyboard()
	)


@dp.callback_query(F.data == "maintenance_check_accounts")
async def cb_maintenance_check_accounts(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	last_edit_at = 0.0
	started_at = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())

	async def progress(progress_data: dict):
		nonlocal last_edit_at
		now = time.monotonic()
		completed = progress_data.get("completed", 0)
		total = progress_data.get("total", 0)
		if completed != total and now - last_edit_at < 2.0:
			return
		last_edit_at = now
		stats = progress_data.get("stats", {})
		tail = [
			format_account_health_result(item)
			for item in progress_data.get("results", [])[-6:]
		]
		await callback.message.edit_text(
			"<b>Проверяю аккаунты</b>\n\n"
			f"{progress_bar(completed, total)} <b>{completed}/{total}</b>\n"
			f"OK/Warn/Bad/Off: <b>{stats.get('ok', 0)}</b> / <b>{stats.get('warning', 0)}</b> / "
			f"<b>{stats.get('bad', 0)}</b> / <b>{stats.get('disabled', 0)}</b>\n\n"
			f"{chr(10).join(tail) if tail else 'Жду первые результаты...'}"
		)

	await callback.answer("Начинаю проверку.")
	result = await run_account_health_check(progress_callback=progress, include_write_probe=False)
	record_health_check_run(
		"accounts_manual",
		started_at,
		"ok",
		f"ok={result['stats']['ok']}, warning={result['stats']['warning']}, bad={result['stats']['bad']}, disabled={result['stats']['disabled']}"
	)
	await callback.message.edit_text(
		"<b>Проверка аккаунтов завершена</b>\n\n"
		f"Всего: <b>{result['total']}</b>\n"
		f"OK: <b>{result['stats']['ok']}</b>\n"
		f"Предупреждения: <b>{result['stats']['warning']}</b>\n"
		f"Проблемные: <b>{result['stats']['bad']}</b>\n"
		f"Выключены: <b>{result['stats']['disabled']}</b>",
		reply_markup=maintenance_menu_keyboard()
	)


@dp.callback_query(F.data == "maintenance_backups")
async def cb_maintenance_backups(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	backups = list_backups(5)
	lines = ["<b>Backups</b>", ""]
	if not backups:
		lines.append("Backup-файлов пока нет.")
	else:
		for item in backups:
			lines.append(f"- <code>{html.escape(item['filename'])}</code> ({human_size(item['size_bytes'])})")
	await callback.message.edit_text("\n".join(lines), reply_markup=backup_menu_keyboard())
	await callback.answer()


async def _create_and_send_backup(callback: CallbackQuery, include_sessions: bool):
	await callback.message.edit_text("Создаю backup, подожди немного...")
	backup = create_backup(include_sessions=include_sessions)
	await bot.send_document(
		callback.message.chat.id,
		FSInputFile(backup["path"], filename=backup["filename"]),
		caption=f"Backup готов: {backup['filename']} ({human_size(backup['size_bytes'])})"
	)
	await callback.message.answer("Готово.", reply_markup=backup_menu_keyboard())


@dp.callback_query(F.data == "maintenance_backup_create_db")
async def cb_maintenance_backup_create_db(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	await callback.answer("Создаю backup.")
	await _create_and_send_backup(callback, include_sessions=False)


@dp.callback_query(F.data == "maintenance_backup_create_sessions")
async def cb_maintenance_backup_create_sessions(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	await callback.answer("Создаю backup с сессиями.")
	await _create_and_send_backup(callback, include_sessions=True)


@dp.callback_query(F.data == "maintenance_backup_download_latest")
async def cb_maintenance_backup_download_latest(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	backup = get_latest_backup()
	if not backup:
		await callback.answer("Backup-файлов нет.", show_alert=True)
		return
	await bot.send_document(
		callback.message.chat.id,
		FSInputFile(backup["path"], filename=backup["filename"]),
		caption=f"Последний backup: {backup['filename']} ({human_size(backup['size_bytes'])})"
	)
	await callback.answer("Отправил.")


@dp.callback_query(F.data == "maintenance_backup_restore_start")
async def cb_maintenance_backup_restore_start(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	await callback.message.edit_text(
		"Отправь backup-файл .zip. Перед восстановлением бот сам сделает pre-restore backup текущей БД.",
		reply_markup=cancel_action_keyboard()
	)
	await state.set_state(MaintenanceStates.WaitingForRestoreBackup)
	await callback.answer()


@dp.message(MaintenanceStates.WaitingForRestoreBackup, F.document)
async def handle_restore_backup_file(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("Нет прав.")
		return
	document = message.document
	if not document.file_name or not document.file_name.endswith(".zip"):
		await message.answer("Нужен backup-файл .zip.", reply_markup=cancel_action_keyboard())
		return

	workspace_id = get_current_workspace_id()
	await message.answer("Восстанавливаю backup: останавливаю клиентов пространства...")
	with tempfile.TemporaryDirectory() as tmp_dir:
		tmp_path = os.path.join(tmp_dir, document.file_name)
		await bot.download(document, destination=tmp_path)
		try:
			await stop_workspace_clients(workspace_id)
			result = restore_backup_file(tmp_path, restore_sessions=True)
			update_all_tables()
			await start_workspace_clients(workspace_id)
			await state.clear()
			await message.answer(
				"Backup восстановлен.\n"
				f"Pre-restore backup: <code>{html.escape(result['pre_restore_backup']['filename'])}</code>",
				reply_markup=backup_menu_keyboard()
			)
		except Exception as e:
			await start_workspace_clients(workspace_id)
			await state.clear()
			await message.answer(
				f"Не удалось восстановить backup: <code>{html.escape(str(e)[:500])}</code>",
				reply_markup=backup_menu_keyboard()
			)


@dp.message(MaintenanceStates.WaitingForRestoreBackup)
async def handle_restore_backup_wrong_file(message: Message, state: FSMContext):
	if not user_is_allowed(message.from_user.id):
		await message.answer("Нет прав.")
		return
	await message.answer("Ожидается документ .zip. Можно отменить действие.", reply_markup=cancel_action_keyboard())


@dp.callback_query(F.data == "maintenance_errors")
async def cb_maintenance_errors(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	errors = list_bot_errors(20)
	lines = ["<b>Журнал ошибок</b>", ""]
	if not errors:
		lines.append("Ошибок пока нет.")
	else:
		for item in errors:
			label = item.get("label") or item.get("session_name") or "-"
			lines.append(
				f"#{item['id']} <b>{html.escape(item['level'])}</b> "
				f"<code>{html.escape(str(item['timestamp']))}</code>\n"
				f"Источник: <code>{html.escape(str(item.get('source') or '-'))}</code> | Акк: <code>{html.escape(str(label))}</code>\n"
				f"{html.escape(str(item.get('message') or '')[:260])}\n"
			)
	text = "\n".join(lines)
	if len(text) > 3900:
		text = text[:3800] + "\n\n...обрезано, ошибок много."
	await callback.message.edit_text(text, reply_markup=errors_menu_keyboard())
	await callback.answer()


@dp.callback_query(F.data == "maintenance_errors_clear_confirm")
async def cb_maintenance_errors_clear_confirm(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	keyboard = InlineKeyboardMarkup(inline_keyboard=[
		[InlineKeyboardButton(text="Да, очистить", callback_data="maintenance_errors_clear")],
		[InlineKeyboardButton(text="Отмена", callback_data="maintenance_errors")]
	])
	await callback.message.edit_text("Очистить журнал ошибок?", reply_markup=keyboard)
	await callback.answer()


@dp.callback_query(F.data == "maintenance_errors_clear")
async def cb_maintenance_errors_clear(callback: CallbackQuery, state: FSMContext):
	if not user_is_allowed(callback.from_user.id):
		await callback.answer("Нет прав.")
		return
	await state.clear()
	count = clear_bot_errors()
	await callback.message.edit_text(f"Очищено ошибок: <b>{count}</b>.", reply_markup=errors_menu_keyboard())
	await callback.answer("Готово.")
