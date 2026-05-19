import html
import logging
import os
import time

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from db import (add_proxies_bulk, delete_all_proxies, delete_expired_proxies,
                delete_proxy, get_db_connection, get_proxy_details,
                get_proxy_summary, list_proxies)
from services.proxy_health import (format_proxy_addr, progress_bar,
                                   proxy_display_status,
                                   run_proxy_health_check)
from services.proxy_parser import parse_proxy_import_line
from userbot import reinitialize_telethon_client, restart_workspace_clients

from ..bot_instance import bot, dp
from ..keyboards import cancel_action_keyboard, main_menu_keyboard
from ..states import AccountAdditionStates
from ..utils import user_is_allowed

logger = logging.getLogger(__name__)


def _parse_proxy_import_line(line: str) -> dict | None:
    return parse_proxy_import_line(line)


def _proxy_display_status(proxy: dict) -> str:
    status = (proxy.get("computed_status") or proxy.get("status") or "active").lower()
    return proxy_display_status(status)


def _proxy_owner_text(proxy: dict) -> str:
    account_id = proxy.get("assigned_account_id")
    if not account_id:
        return "свободен"
    label = proxy.get("account_label") or proxy.get("account_session_name") or proxy.get("account_phone") or "аккаунт"
    return f"{label} (ID {account_id})"


def _proxy_management_keyboard(page: int, total_pages: int, proxies_on_page: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for proxy in proxies_on_page:
        buttons.append([
            InlineKeyboardButton(
                text=f"Удалить #{proxy['id']}",
                callback_data=f"proxy_pool_delete_confirm:{proxy['id']}:{page}"
            )
        ])

    pagination = []
    if page > 1:
        pagination.append(InlineKeyboardButton(text="Назад", callback_data=f"proxy_pool_page:{page - 1}"))
    pagination.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="ignore"))
    if page < total_pages:
        pagination.append(InlineKeyboardButton(text="Вперед", callback_data=f"proxy_pool_page:{page + 1}"))
    if pagination:
        buttons.append(pagination)

    buttons.append([InlineKeyboardButton(text="Проверить все прокси", callback_data="proxy_pool_check_all")])
    buttons.append([InlineKeyboardButton(text="Добавить прокси файлом", callback_data="bulk_add_proxies_start")])
    buttons.append([InlineKeyboardButton(text="Удалить истекшие", callback_data="proxy_pool_delete_expired_confirm")])
    buttons.append([InlineKeyboardButton(text="Удалить все", callback_data="proxy_pool_delete_all_confirm")])
    buttons.append([InlineKeyboardButton(text="Назад в настройки аккаунтов", callback_data="account_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _build_proxy_management_text(page: int) -> tuple[str, InlineKeyboardMarkup]:
    summary = get_proxy_summary()
    proxies_on_page, page, total_pages = list_proxies(page=page, per_page=5)
    lines = [
        "<b>Управление прокси</b>",
        "",
        f"Всего: <b>{summary['total']}</b>",
        f"Свободно: <b>{summary['free']}</b>",
        f"Занято: <b>{summary['busy']}</b>",
        f"Истекло: <b>{summary['expired']}</b>",
        f"Не работает: <b>{summary['failed']}</b>",
        "",
    ]

    if not proxies_on_page:
        lines.append("В этом пространстве пока нет прокси.")
    else:
        for proxy in proxies_on_page:
            username = proxy.get("proxy_username") or "-"
            password = "********" if proxy.get("proxy_password") else "-"
            expires_at = proxy.get("expires_at") or "не указан"
            last_checked = proxy.get("last_checked_at") or "не проверялся"
            last_error = proxy.get("last_error") or "-"
            proxy_addr = format_proxy_addr(proxy)
            lines.extend([
                f"<b>#{proxy['id']}</b> <code>{html.escape(proxy_addr)}</code>",
                f"Статус: <b>{html.escape(_proxy_display_status(proxy))}</b>",
                f"Занят: <code>{html.escape(_proxy_owner_text(proxy))}</code>",
                f"Логин: <code>{html.escape(str(username))}</code> | Пароль: <code>{password}</code>",
                f"Истекает: <code>{html.escape(str(expires_at))}</code>",
                f"Проверка: <code>{html.escape(str(last_checked))}</code>",
                f"Ошибка: <code>{html.escape(str(last_error)[:120])}</code>",
                "",
            ])

    return "\n".join(lines), _proxy_management_keyboard(page, total_pages, proxies_on_page)


def _get_expired_proxy_account_ids() -> list[int]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT assigned_account_id
        FROM proxies
        WHERE assigned_account_id IS NOT NULL
          AND (
              COALESCE(status, 'active') = 'expired'
              OR (expires_at IS NOT NULL AND expires_at != '' AND datetime(expires_at) <= datetime('now'))
          )
    """)
    account_ids = [row["assigned_account_id"] for row in cursor.fetchall()]
    conn.close()
    return account_ids


def _get_all_proxy_account_ids() -> list[int]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT assigned_account_id FROM proxies WHERE assigned_account_id IS NOT NULL")
    account_ids = [row["assigned_account_id"] for row in cursor.fetchall()]
    conn.close()
    return account_ids


@dp.callback_query(F.data == "bulk_add_proxies_start")
async def cb_bulk_add_proxies_start(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await callback.message.edit_text(
        "Отправьте .txt файл с прокси.\n"
        "Форматы по строкам:\n"
        "<code>IP:PORT</code>\n"
        "<code>IP:PORT:USERNAME:PASSWORD</code>\n"
        "<code>IP:PORT:USERNAME:PASSWORD:YYYY-MM-DD</code>\n"
        "<code>socks5:IP:PORT:USERNAME:PASSWORD:YYYY-MM-DD</code>\n"
        "Для IPv6 используйте квадратные скобки: <code>[IPv6]:PORT:USER:PASS</code>.",
        reply_markup=cancel_action_keyboard()
    )
    await state.set_state(AccountAdditionStates.WaitingForProxyFile)
    await callback.answer()


@dp.message(AccountAdditionStates.WaitingForProxyFile, F.document)
async def handle_proxy_file(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    if not message.document.file_name.endswith(".txt"):
        await message.answer("Пожалуйста, загрузите файл в формате .txt")
        return

    await message.answer("Обрабатываю файл...")
    file_path = f"temp_proxies_{message.from_user.id}.txt"
    try:
        await bot.download(message.document, destination=file_path)
        proxies_to_add = []
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed_proxy = _parse_proxy_import_line(line)
                    if parsed_proxy:
                        proxies_to_add.append(parsed_proxy)
                except (TypeError, ValueError):
                    continue
        if not proxies_to_add:
            await message.answer("В файле не найдено прокси в корректном формате.")
            return

        added, skipped = add_proxies_bulk(proxies_to_add)
        await message.answer(
            f"✅ Готово!\n- Добавлено новых прокси: {added}\n- Пропущено (дубликаты): {skipped}",
            reply_markup=main_menu_keyboard()
        )

    except Exception as e:
        await message.answer(f"Произошла ошибка при обработке файла: {e}")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
        await state.clear()


@dp.message(AccountAdditionStates.WaitingForProxyFile, F.text, ~F.text.startswith('/'))
async def handle_proxy_file_incorrectly(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    await message.answer("Пожалуйста, отправьте документ (.txt файл), а не текст.")


@dp.callback_query(F.data == "proxy_management")
async def cb_proxy_management(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await state.clear()
    text, keyboard = _build_proxy_management_text(page=1)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data.startswith("proxy_pool_page:"))
async def cb_proxy_pool_page(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    try:
        page = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        page = 1
    text, keyboard = _build_proxy_management_text(page=page)
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()


@dp.callback_query(F.data == "proxy_pool_check_all")
async def cb_proxy_pool_check_all(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    await state.clear()
    last_edit_at = 0.0

    async def edit_progress(progress: dict, force: bool = False):
        nonlocal last_edit_at
        now = time.monotonic()
        if not force and now - last_edit_at < 1.5:
            return
        last_edit_at = now
        total = progress.get("total", 0)
        completed = progress.get("completed", 0)
        stats = progress.get("stats", {})
        result_lines = progress.get("result_lines", [])
        tail_lines = []
        for item in result_lines[-8:]:
            tail_lines.append(
                f"#{item['id']} <code>{html.escape(item['addr'])}</code> - "
                f"<b>{html.escape(proxy_display_status(item['status']))}</b>: "
                f"<code>{html.escape(str(item['text'])[:140])}</code>"
            )
        tail = "\n".join(tail_lines)
        try:
            await callback.message.edit_text(
                "<b>Проверяю прокси</b>\n\n"
                f"{progress_bar(completed, total)} <b>{completed}/{total}</b>\n"
                f"Рабочие: <b>{stats.get('active', 0)}</b>\n"
                f"Не работают: <b>{stats.get('failed', 0)}</b>\n"
                f"Истекли: <b>{stats.get('expired', 0)}</b>\n\n"
                f"{tail or 'Жду первые результаты...'}"
            )
        except Exception as e:
            logger.debug(f"Failed to update proxy check progress: {e}")

    await callback.answer("Начинаю проверку.")
    result = await run_proxy_health_check(
        concurrency=5,
        progress_callback=lambda progress: edit_progress(
            progress,
            force=progress.get("completed", 0) == progress.get("total", 0)
        )
    )

    total = result["total"]
    if total == 0:
        text, keyboard = _build_proxy_management_text(page=1)
        await callback.message.edit_text("Прокси для проверки пока нет.\n\n" + text, reply_markup=keyboard)
        return

    affected_accounts = result["affected_accounts"]
    stats = result["stats"]
    if affected_accounts:
        await callback.message.edit_text(
            "<b>Проверка прокси завершена.</b>\n\n"
            "Нашел плохие прокси у активных аккаунтов, перезапускаю клиентов пространства..."
        )
        await restart_workspace_clients()

    text, keyboard = _build_proxy_management_text(page=1)
    affected_text = (
        f"\nАккаунтов на плохих/истекших прокси: <b>{len(affected_accounts)}</b>. "
        "Они не будут запущены, пока прокси не станет рабочим или не будет заменен.\n"
        if affected_accounts else ""
    )
    await callback.message.edit_text(
        "<b>Проверка прокси завершена</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"Рабочие: <b>{stats['active']}</b>\n"
        f"Не работают: <b>{stats['failed']}</b>\n"
        f"Истекли: <b>{stats['expired']}</b>\n"
        f"{affected_text}\n"
        f"{text}",
        reply_markup=keyboard
    )


@dp.callback_query(F.data.startswith("proxy_pool_delete_confirm:"))
async def cb_proxy_pool_delete_confirm(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    try:
        _, proxy_id_raw, page_raw = callback.data.split(":")
        proxy_id = int(proxy_id_raw)
        page = int(page_raw)
    except (ValueError, IndexError):
        await callback.answer("Некорректный ID прокси.", show_alert=True)
        return

    proxy = get_proxy_details(proxy_id)
    if not proxy:
        await callback.answer("Прокси уже не найден.", show_alert=True)
        text, keyboard = _build_proxy_management_text(page=page)
        await callback.message.edit_text(text, reply_markup=keyboard)
        return

    proxy_addr = f"{proxy.get('proxy_type') or 'socks5'}://{proxy.get('proxy_ip')}:{proxy.get('proxy_port')}"
    owner = _proxy_owner_text(proxy)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да, удалить", callback_data=f"proxy_pool_delete_execute:{proxy_id}:{page}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"proxy_pool_page:{page}")]
    ])
    await callback.message.edit_text(
        f"Удалить прокси <code>{html.escape(proxy_addr)}</code>?\n"
        f"Занят: <code>{html.escape(owner)}</code>\n\n"
        "Если прокси привязан к аккаунту, у аккаунта будут очищены настройки прокси.",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("proxy_pool_delete_execute:"))
async def cb_proxy_pool_delete_execute(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    try:
        _, proxy_id_raw, page_raw = callback.data.split(":")
        proxy_id = int(proxy_id_raw)
        page = int(page_raw)
    except (ValueError, IndexError):
        await callback.answer("Некорректный ID прокси.", show_alert=True)
        return

    proxy = get_proxy_details(proxy_id)
    assigned_account_id = proxy.get("assigned_account_id") if proxy else None
    deleted = delete_proxy(proxy_id)
    if assigned_account_id:
        await reinitialize_telethon_client(int(assigned_account_id))
    text, keyboard = _build_proxy_management_text(page=page)
    prefix = "✅ Прокси удален.\n\n" if deleted else "⚠️ Прокси не найден или не удален.\n\n"
    await callback.message.edit_text(prefix + text, reply_markup=keyboard)
    await callback.answer("Готово." if deleted else "Не найден.")


@dp.callback_query(F.data == "proxy_pool_delete_expired_confirm")
async def cb_proxy_pool_delete_expired_confirm(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    summary = get_proxy_summary()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Да, удалить истекшие ({summary['expired']})", callback_data="proxy_pool_delete_expired_execute")],
        [InlineKeyboardButton(text="Отмена", callback_data="proxy_management")]
    ])
    await callback.message.edit_text(
        f"Удалить все истекшие прокси в текущем пространстве?\n"
        f"Истекших прокси: <b>{summary['expired']}</b>\n\n"
        "У аккаунтов, которые использовали эти прокси, настройки прокси будут очищены.",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data == "proxy_pool_delete_expired_execute")
async def cb_proxy_pool_delete_expired_execute(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    affected_accounts = _get_expired_proxy_account_ids()
    deleted_count = delete_expired_proxies()
    if affected_accounts:
        await restart_workspace_clients()
    text, keyboard = _build_proxy_management_text(page=1)
    await callback.message.edit_text(
        f"✅ Истекшие прокси удалены: <b>{deleted_count}</b>.\n\n{text}",
        reply_markup=keyboard
    )
    await callback.answer("Готово.")


@dp.callback_query(F.data == "proxy_pool_delete_all_confirm")
async def cb_proxy_pool_delete_all_confirm(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    summary = get_proxy_summary()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Да, удалить все ({summary['total']})", callback_data="proxy_pool_delete_all_execute")],
        [InlineKeyboardButton(text="Отмена", callback_data="proxy_management")]
    ])
    await callback.message.edit_text(
        f"Удалить <b>все</b> прокси в текущем пространстве?\n"
        f"Всего прокси: <b>{summary['total']}</b>\n\n"
        "Это также очистит прокси у всех аккаунтов этого пространства.",
        reply_markup=keyboard
    )
    await callback.answer()


@dp.callback_query(F.data == "proxy_pool_delete_all_execute")
async def cb_proxy_pool_delete_all_execute(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    affected_accounts = _get_all_proxy_account_ids()
    deleted_count = delete_all_proxies()
    if affected_accounts:
        await restart_workspace_clients()
    text, keyboard = _build_proxy_management_text(page=1)
    await callback.message.edit_text(
        f"✅ Все прокси удалены: <b>{deleted_count}</b>.\n\n{text}",
        reply_markup=keyboard
    )
    await callback.answer("Готово.")

