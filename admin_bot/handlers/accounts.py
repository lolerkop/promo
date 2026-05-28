import html
import json
import logging
import os
import shutil
import sqlite3
import time

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from telethon import TelegramClient
from telethon.errors import (ApiIdInvalidError, FloodWaitError,
                             PhoneCodeEmptyError, PhoneCodeExpiredError,
                             PhoneCodeHashEmptyError, PhoneCodeInvalidError,
                             PhoneNumberBannedError, PhoneNumberFloodError,
                             PhoneNumberInvalidError, PhonePasswordFloodError,
                             SessionPasswordNeededError)
from telethon.sessions import SQLiteSession
from telethon.tl.functions.account import SetPrivacyRequest
from telethon.tl.types import (InputPrivacyKeyPhoneNumber,
                               InputPrivacyValueDisallowAll)

from db import (create_account_with_proxy, get_account_details,
                get_active_workspace_id, get_db_connection,
                get_workspace_session_dir,
                remove_telethon_account)
from services.account_health import (format_account_health_result,
                                     run_account_health_check)
from services.api_credentials import (DEFAULT_MAX_ACCOUNTS_PER_API,
                                      add_api_credential,
                                      delete_api_credential,
                                      get_available_api_credential,
                                      list_api_credentials,
                                      set_api_credential_status)
from services.proxy_health import progress_bar
from services.proxy_repository import get_unassigned_proxy
from userbot import (attach_authorized_client_to_runtime,
                     reinitialize_telethon_client, remove_client_from_runtime)

from ..bot_instance import bot, dp, global_reg_cache
from ..keyboards import main_menu_keyboard
from ..states import (AccountAdditionStates, AccountManagementStates,
                      ApiCredentialStates)
from ..utils import show_accounts_list, user_is_allowed

logger = logging.getLogger(__name__)


def _api_credentials_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Добавить API ID/HASH", callback_data="api_credential_add")]]
    for credential in list_api_credentials():
        status = "🟢" if credential.get("is_active") else "🔴"
        label = credential.get("label") or f"API {credential.get('api_id')}"
        used = int(credential.get("accounts_count") or 0)
        max_accounts = int(credential.get("max_accounts") or DEFAULT_MAX_ACCOUNTS_PER_API)
        rows.append([
            InlineKeyboardButton(
                text=f"{status} {label[:18]} {used}/{max_accounts}",
                callback_data=f"api_credential_ignore:{credential['id']}"
            )
        ])
        rows.append([
            InlineKeyboardButton(
                text="Выключить" if credential.get("is_active") else "Включить",
                callback_data=f"api_credential_toggle:{credential['id']}"
            ),
            InlineKeyboardButton(text="Удалить", callback_data=f"api_credential_delete:{credential['id']}")
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="account_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_api_credentials_menu() -> str:
    credentials = list_api_credentials()
    if not credentials:
        return (
            "<b>API ID/HASH</b>\n\n"
            "Пары API пока не добавлены. Добавь хотя бы одну пару, и при входе аккаунтов бот будет сам выбирать "
            f"самую свободную. По умолчанию лимит: {DEFAULT_MAX_ACCOUNTS_PER_API} аккаунтов на одну пару."
        )

    lines = [
        "<b>API ID/HASH</b>",
        "",
        "Бот выбирает активную пару с минимальной нагрузкой.",
        "",
    ]
    for credential in credentials:
        status = "active" if credential.get("is_active") else "disabled"
        used = int(credential.get("accounts_count") or 0)
        max_accounts = int(credential.get("max_accounts") or DEFAULT_MAX_ACCOUNTS_PER_API)
        label = html.escape(credential.get("label") or f"API {credential.get('api_id')}")
        lines.append(
            f"ID {credential['id']}: <b>{label}</b> | API <code>{credential['api_id']}</code> | "
            f"{used}/{max_accounts} | {status}"
        )
    return "\n".join(lines)


def _pick_api_credentials_for_new_account() -> dict | None:
    return get_available_api_credential()


def _safe_session_label(source_name: str) -> str:
    base_name = os.path.splitext(os.path.basename(source_name or ""))[0].strip()
    safe = "".join(ch if (ch.isalnum() or ch in ("_", "-")) else "_" for ch in base_name)
    return (safe or f"session_{int(time.time())}")[:64]


def _session_label_exists(label: str) -> bool:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM accounts WHERE session_name = ? LIMIT 1", (label,))
        if cursor.fetchone():
            return True
    finally:
        conn.close()
    return os.path.exists(os.path.join(get_workspace_session_dir(), f"{label}.session"))


def _unique_session_label(source_name: str) -> str:
    base_label = _safe_session_label(source_name)
    label = base_label
    suffix = 2
    while _session_label_exists(label):
        label = f"{base_label}_{suffix}"
        suffix += 1
    return label


def _find_existing_account_by_user_id(user_id: int) -> dict | None:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, session_name, label FROM accounts WHERE user_id = ? LIMIT 1", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _proxy_payload_from_row(proxy_row: dict | None) -> tuple[dict | None, dict]:
    if not proxy_row:
        return None, {
            "proxy_id": None,
            "proxy_type": None,
            "proxy_ip": None,
            "proxy_port": None,
            "proxy_username": None,
            "proxy_password": None,
        }

    runtime_proxy = {
        "proxy_type": proxy_row["proxy_type"],
        "addr": proxy_row["proxy_ip"],
        "port": proxy_row["proxy_port"],
        "username": proxy_row.get("proxy_username"),
        "password": proxy_row.get("proxy_password"),
    }
    fsm_proxy = {
        "proxy_id": proxy_row["id"],
        "proxy_type": proxy_row["proxy_type"],
        "proxy_ip": proxy_row["proxy_ip"],
        "proxy_port": proxy_row["proxy_port"],
        "proxy_username": proxy_row.get("proxy_username"),
        "proxy_password": proxy_row.get("proxy_password"),
    }
    return runtime_proxy, fsm_proxy


async def _import_telethon_session_file(
        source_session_path: str,
        source_name: str,
        message: Message,
        state: FSMContext,
        json_data: dict | None = None
) -> tuple[bool, str]:
    json_data = json_data or {}
    credential = _pick_api_credentials_for_new_account()
    if credential:
        api_id = int(credential["api_id"])
        api_hash = credential["api_hash"]
    else:
        api_id = json_data.get("app_id") or json_data.get("api_id")
        api_hash = json_data.get("app_hash") or json_data.get("api_hash")

    if not api_id or not api_hash:
        return False, "нет свободной API-пары и нет app_id/app_hash в JSON"

    label = _unique_session_label(source_name)
    final_session_path = os.path.join(get_workspace_session_dir(), f"{label}.session")
    free_proxy = get_unassigned_proxy()
    runtime_proxy, fsm_proxy = _proxy_payload_from_row(free_proxy)
    client = None

    try:
        shutil.copy(source_session_path, final_session_path)
        client = TelegramClient(SQLiteSession(final_session_path), int(api_id), api_hash, proxy=runtime_proxy)
        await client.connect()

        if not await client.is_user_authorized():
            if client.is_connected():
                await client.disconnect()
            try:
                os.remove(final_session_path)
            except OSError:
                pass
            return False, "сессия не авторизована"

        me = await client.get_me()
        existing = _find_existing_account_by_user_id(me.id)
        if existing:
            if client.is_connected():
                await client.disconnect()
            try:
                os.remove(final_session_path)
            except OSError:
                pass
            return False, f"аккаунт уже есть в базе: ID {existing['id']} ({existing['session_name']})"

        phone = json_data.get("phone") or getattr(me, "phone", None) or f"uid_{me.id}"
        fsm_data = {
            "phone": phone,
            "api_id": int(api_id),
            "api_hash": api_hash,
            "label": label,
            **fsm_proxy,
        }
        await finalize_account_add(client, fsm_data, message, state)
        return True, f"{label} / UID {me.id}"
    except Exception as e:
        if client and client.is_connected():
            await client.disconnect()
        try:
            if os.path.exists(final_session_path):
                os.remove(final_session_path)
        except OSError:
            pass
        return False, f"{type(e).__name__}: {e}"


@dp.callback_query(F.data == "account_settings")
async def cb_account_settings(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗂️ Сканировать и импортировать сессии", callback_data="account_scan_and_import")],
        [InlineKeyboardButton(text="📥 Загрузить .session Telethon", callback_data="account_import_telethon_session")],
        [InlineKeyboardButton(text="➕ Добавить по номеру телефона", callback_data="account_add")],
        [InlineKeyboardButton(text="🔑 API ID/HASH", callback_data="api_credentials_menu")],
        [InlineKeyboardButton(text="🗂️ Массовое добавление прокси", callback_data="bulk_add_proxies_start")],
        [InlineKeyboardButton(text="📋 Управление прокси", callback_data="proxy_management")],
        [InlineKeyboardButton(text="❌ Удалить аккаунт", callback_data="account_remove_info")],
        [InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="account_list")],
        [InlineKeyboardButton(text="💬 Автоответчик ЛС", callback_data="auto_responder_menu")],
        [InlineKeyboardButton(text="🚀 Массовое обновление профилей", callback_data="mass_profile_update")],
        [InlineKeyboardButton(text="🚦 Проверить все сессии", callback_data="check_all_sessions")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
    ])
    await callback.message.edit_text("⚙️ <b>Настройки аккаунтов</b>:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "api_credentials_menu")
async def cb_api_credentials_menu(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await state.clear()
    await callback.message.edit_text(_format_api_credentials_menu(), reply_markup=_api_credentials_menu_keyboard())
    await callback.answer()


@dp.callback_query(F.data == "api_credential_add")
async def cb_api_credential_add(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await state.clear()
    await callback.message.edit_text(
        "Введите короткое имя для API-пары, например: main-1",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="api_credentials_menu")]]
        )
    )
    await state.set_state(ApiCredentialStates.WaitingForLabel)
    await callback.answer()


@dp.message(ApiCredentialStates.WaitingForLabel, F.text, ~F.text.startswith('/'))
async def handle_api_credential_label(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        return
    label = message.text.strip()
    if not label:
        await message.answer("Имя не должно быть пустым.")
        return
    await state.update_data(api_credential_label=label)
    await message.answer("Введите API_ID числом.")
    await state.set_state(ApiCredentialStates.WaitingForApiId)


@dp.message(ApiCredentialStates.WaitingForApiId, F.text, ~F.text.startswith('/'))
async def handle_api_credential_api_id(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        return
    if not message.text.strip().isdigit():
        await message.answer("API_ID должен быть числом.")
        return
    await state.update_data(api_credential_api_id=int(message.text.strip()))
    await message.answer("Введите API_HASH.")
    await state.set_state(ApiCredentialStates.WaitingForApiHash)


@dp.message(ApiCredentialStates.WaitingForApiHash, F.text, ~F.text.startswith('/'))
async def handle_api_credential_api_hash(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        return
    api_hash = message.text.strip()
    if not api_hash:
        await message.answer("API_HASH не должен быть пустым.")
        return
    await state.update_data(api_credential_api_hash=api_hash)
    await message.answer(f"Введите лимит аккаунтов на эту пару. По умолчанию: {DEFAULT_MAX_ACCOUNTS_PER_API}.")
    await state.set_state(ApiCredentialStates.WaitingForMaxAccounts)


@dp.message(ApiCredentialStates.WaitingForMaxAccounts, F.text, ~F.text.startswith('/'))
async def handle_api_credential_max_accounts(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        return
    raw_value = message.text.strip()
    if raw_value and not raw_value.isdigit():
        await message.answer("Лимит должен быть числом.")
        return
    max_accounts = int(raw_value) if raw_value else DEFAULT_MAX_ACCOUNTS_PER_API
    data = await state.get_data()
    try:
        add_api_credential(
            api_id=data["api_credential_api_id"],
            api_hash=data["api_credential_api_hash"],
            label=data.get("api_credential_label"),
            max_accounts=max_accounts,
        )
    except sqlite3.IntegrityError:
        await message.answer("Такая API-пара уже есть в этом пространстве.", reply_markup=_api_credentials_menu_keyboard())
    except Exception as e:
        await message.answer(f"Не удалось добавить API-пару: {e}", reply_markup=_api_credentials_menu_keyboard())
    else:
        await message.answer("API-пара добавлена.", reply_markup=_api_credentials_menu_keyboard())
    await state.clear()


@dp.callback_query(F.data.startswith("api_credential_toggle:"))
async def cb_api_credential_toggle(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    try:
        credential_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ID.", show_alert=True)
        return
    credential = next((item for item in list_api_credentials() if item["id"] == credential_id), None)
    if not credential:
        await callback.answer("API-пара не найдена.", show_alert=True)
        return
    set_api_credential_status(credential_id, not bool(credential.get("is_active")))
    await callback.message.edit_text(_format_api_credentials_menu(), reply_markup=_api_credentials_menu_keyboard())
    await callback.answer("Статус обновлен.")


@dp.callback_query(F.data.startswith("api_credential_delete:"))
async def cb_api_credential_delete(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    try:
        credential_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ID.", show_alert=True)
        return
    ok, message_text = delete_api_credential(credential_id)
    await callback.message.edit_text(_format_api_credentials_menu(), reply_markup=_api_credentials_menu_keyboard())
    await callback.answer(message_text, show_alert=not ok)


@dp.callback_query(F.data.startswith("api_credential_ignore:"))
async def cb_api_credential_ignore(callback: CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "account_import_telethon_session")
async def cb_import_telethon_session(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await state.clear()
    await callback.message.edit_text(
        "Загрузите файл <code>.session</code> от Telethon. "
        "Бот возьмет свободную API-пару из текущего пространства и проверит сессию.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="account_settings")]]
        )
    )
    await state.set_state(AccountAdditionStates.WaitingForTelethonSessionFile)
    await callback.answer()


@dp.message(AccountAdditionStates.WaitingForTelethonSessionFile, F.document)
async def handle_telethon_session_upload(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        return
    document_name = message.document.file_name or ""
    if not document_name.endswith(".session"):
        await message.answer("Нужен файл Telethon с расширением .session.")
        return

    temp_dir = os.path.join("sessions_import", "uploads")
    os.makedirs(temp_dir, exist_ok=True)
    temp_path = os.path.join(temp_dir, f"upload_{message.from_user.id}_{int(time.time())}.session")
    await bot.download(message.document.file_id, destination=temp_path)
    await message.answer(f"Проверяю сессию <code>{html.escape(document_name)}</code>...")

    try:
        ok, details = await _import_telethon_session_file(temp_path, document_name, message, state)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass

    if ok:
        await message.answer(f"Импорт .session завершен: {html.escape(details)}", reply_markup=main_menu_keyboard())
    else:
        await message.answer(f"Не удалось импортировать .session: {html.escape(details)}", reply_markup=main_menu_keyboard())
    await state.clear()


@dp.message(AccountAdditionStates.WaitingForTelethonSessionFile)
async def handle_telethon_session_upload_wrong(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id):
        return
    await message.answer("Ожидается документ .session или нажмите отмену.")


@dp.callback_query(F.data == "account_scan_and_import")
async def cb_scan_and_import_sessions(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return

    await callback.message.edit_text("🔎 Начинаю сканирование папки `sessions_import`...")
    await callback.answer()

    SESSIONS_IMPORT_DIR = "sessions_import"
    SESSIONS_DIR = get_workspace_session_dir()
    SESSIONS_ARCHIVE_DIR = os.path.join(SESSIONS_IMPORT_DIR, "archive")

    os.makedirs(SESSIONS_ARCHIVE_DIR, exist_ok=True)

    try:
        import_files = os.listdir(SESSIONS_IMPORT_DIR)
        json_files = [f for f in import_files if f.endswith('.json')]
        paired_session_files = {f.replace('.json', '.session') for f in json_files}
        session_only_files = [
            f for f in import_files
            if f.endswith('.session') and f not in paired_session_files
        ]
    except FileNotFoundError:
        await bot.send_message(callback.from_user.id,
                               "⚠️ Папка `sessions_import` не найдена. Создайте ее и поместите файлы.")
        return

    if not json_files and not session_only_files:
        await bot.send_message(callback.from_user.id,
                               "ℹ️ Новых `.json` или `.session` файлов для импорта в папке `sessions_import` не найдено.")
        return

    conn_check = get_db_connection()
    cursor_check = conn_check.cursor()
    cursor_check.execute("SELECT phone FROM accounts")
    existing_phones = {row['phone'] for row in cursor_check.fetchall()}
    conn_check.close()

    success_count = 0
    skipped_count = 0
    error_count = 0

    for json_filename in json_files:
        json_path = os.path.join(SESSIONS_IMPORT_DIR, json_filename)
        session_filename = json_filename.replace('.json', '.session')
        session_path = os.path.join(SESSIONS_IMPORT_DIR, session_filename)
        client = None

        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            phone = data.get("phone")
            if not phone:
                await bot.send_message(callback.from_user.id,
                                       f"⚠️ Пропуск `{json_filename}`: отсутствует номер телефона в JSON.")
                skipped_count += 1
                continue

            if phone in existing_phones:
                await bot.send_message(callback.from_user.id,
                                       f"ℹ️ Пропуск `{json_filename}`: аккаунт с телефоном {phone} уже существует в базе.")
                shutil.move(json_path, os.path.join(SESSIONS_ARCHIVE_DIR, json_filename))
                if os.path.exists(session_path):
                    shutil.move(session_path, os.path.join(SESSIONS_ARCHIVE_DIR, session_filename))
                skipped_count += 1
                continue

            if not os.path.exists(session_path):
                await bot.send_message(callback.from_user.id,
                                       f"❌ Ошибка для `{json_filename}`: не найден парный файл `{session_filename}`.")
                error_count += 1
                continue

            await bot.send_message(callback.from_user.id, f"▶️ Начинаю импорт аккаунта: {phone}")

            credential = _pick_api_credentials_for_new_account()
            if credential:
                api_id = int(credential["api_id"])
                api_hash = credential["api_hash"]
            else:
                api_id = data.get("app_id") or data.get("api_id")
                api_hash = data.get("app_hash") or data.get("api_hash")
            if not api_id or not api_hash:
                await bot.send_message(
                    callback.from_user.id,
                    f"❌ Ошибка для `{json_filename}`: нет свободной API-пары в боте и нет app_id/app_hash в JSON."
                )
                error_count += 1
                continue
            two_fa_pass = data.get("twoFA")
            label = f"acc_{phone}"

            free_proxy = get_unassigned_proxy()
            proxy_details_runtime = None
            fsm_data = {
                "phone": phone, "api_id": api_id, "api_hash": api_hash, "label": label
            }

            if free_proxy:
                await bot.send_message(callback.from_user.id,
                                       f"Найден свободный прокси: {free_proxy['proxy_ip']}:{free_proxy['proxy_port']}. Применяю...")
                proxy_details_runtime = {
                    "proxy_type": free_proxy['proxy_type'],
                    "addr": free_proxy['proxy_ip'],
                    "port": free_proxy['proxy_port'],
                    "username": free_proxy.get('proxy_username'),
                    "password": free_proxy.get('proxy_password')
                }
                fsm_data.update({
                    "proxy_id": free_proxy['id'],
                    "proxy_type": free_proxy['proxy_type'],
                    "proxy_ip": free_proxy['proxy_ip'],
                    "proxy_port": free_proxy['proxy_port'],
                    "proxy_username": free_proxy.get('proxy_username'),
                    "proxy_password": free_proxy.get('proxy_password')
                })

            final_session_path = os.path.join(SESSIONS_DIR, f"{label}.session")
            shutil.copy(session_path, final_session_path)

            client = TelegramClient(SQLiteSession(final_session_path), int(api_id), api_hash,
                                    proxy=proxy_details_runtime)
            await client.connect()

            if not await client.is_user_authorized():
                if two_fa_pass:
                    await client.sign_in(phone=phone, password=str(two_fa_pass))
                else:
                    await bot.send_message(callback.from_user.id,
                                           f"⚠️ Сессия для {phone} не авторизована, а пароля 2FA нет. Требуется ручная авторизация.")
                    error_count += 1
                    if client: await client.disconnect()
                    continue

            if not await client.is_user_authorized():
                await bot.send_message(callback.from_user.id,
                                       f"❌ Не удалось авторизоваться для {phone} даже с паролем 2FA.")
                error_count += 1
                if client: await client.disconnect()
                continue

            await finalize_account_add(client, fsm_data, callback.message, state)
            success_count += 1

            shutil.move(json_path, os.path.join(SESSIONS_ARCHIVE_DIR, json_filename))
            shutil.move(session_path, os.path.join(SESSIONS_ARCHIVE_DIR, session_filename))

        except Exception as e:
            await bot.send_message(callback.from_user.id,
                                   f"❌ Критическая ошибка при обработке файла `{json_filename}`: {e}")
            logger.error(f"Critical error processing {json_filename}: {e}", exc_info=True)
            error_count += 1
            if client and client.is_connected():
                await client.disconnect()

    for session_filename in session_only_files:
        session_path = os.path.join(SESSIONS_IMPORT_DIR, session_filename)
        await bot.send_message(callback.from_user.id, f"▶️ Импортирую Telethon .session: <code>{html.escape(session_filename)}</code>")
        ok, details = await _import_telethon_session_file(session_path, session_filename, callback.message, state)
        if ok:
            success_count += 1
            try:
                shutil.move(session_path, os.path.join(SESSIONS_ARCHIVE_DIR, session_filename))
            except Exception as e_move:
                logger.warning(f"Could not archive imported session {session_filename}: {e_move}")
        elif "аккаунт уже есть" in details:
            skipped_count += 1
            try:
                shutil.move(session_path, os.path.join(SESSIONS_ARCHIVE_DIR, session_filename))
            except Exception as e_move:
                logger.warning(f"Could not archive skipped session {session_filename}: {e_move}")
        else:
            error_count += 1
        await bot.send_message(callback.from_user.id, f"{'✅' if ok else '⚠️'} {html.escape(session_filename)}: {html.escape(details)}")

    summary_message = f"🏁 Сканирование завершено!\n\n" \
                      f"✅ Успешно импортировано: {success_count}\n" \
                      f"ℹ️ Пропущено (дубликаты): {skipped_count}\n" \
                      f"❌ Ошибок: {error_count}"
    await bot.send_message(callback.from_user.id, summary_message)


@dp.callback_query(F.data == "account_add")
async def cb_account_add(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await state.clear()
    logger.info(f"Admin {callback.from_user.id}: Starting new account addition. State cleared.")
    await callback.message.edit_text("Введите телефон (например, +1234567890).")
    await state.set_state(AccountAdditionStates.WaitingForPhone)
    await callback.answer()


@dp.message(AccountAdditionStates.WaitingForPhone, F.text, ~F.text.startswith('/'))
async def handle_account_add_phone(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    await state.update_data(phone=message.text.strip())
    logger.info(f"Admin {message.from_user.id}: State after phone: {await state.get_data()}")
    credential = _pick_api_credentials_for_new_account()
    if credential:
        await state.update_data(
            api_id=int(credential["api_id"]),
            api_hash=credential["api_hash"],
            api_credential_id=credential["id"],
        )
        used = int(credential.get("accounts_count") or 0)
        max_accounts = int(credential.get("max_accounts") or DEFAULT_MAX_ACCOUNTS_PER_API)
        label = html.escape(credential.get("label") or f"API {credential['api_id']}")
        await message.answer(
            f"Использую API-пару <b>{label}</b>: {used}/{max_accounts}. "
            "Введите метку аккаунта (уникальное имя сессии, например account1)."
        )
        await state.set_state(AccountAdditionStates.WaitingForLabel)
        return

    await message.answer(
        "Свободных API ID/HASH в этом пространстве нет. Добавьте API-пару в разделе аккаунтов или введите API_ID вручную."
    )
    await state.set_state(AccountAdditionStates.WaitingForApiId)


@dp.message(AccountAdditionStates.WaitingForApiId, F.text, ~F.text.startswith('/'))
async def handle_account_add_api_id(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    if not message.text.strip().isdigit():
        await message.answer("API_ID должно быть числом. Повторите ввод.")
        return
    await state.update_data(api_id=int(message.text.strip()))
    logger.info(f"Admin {message.from_user.id}: State after api_id: {await state.get_data()}")
    await message.answer("Введите API_HASH:")
    await state.set_state(AccountAdditionStates.WaitingForApiHash)


@dp.message(AccountAdditionStates.WaitingForApiHash, F.text, ~F.text.startswith('/'))
async def handle_account_add_api_hash(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    await state.update_data(api_hash=message.text.strip())
    logger.info(f"Admin {message.from_user.id}: State after api_hash: {await state.get_data()}")
    await message.answer("Введите метку (label) для аккаунта (уникальное имя сессии, например 'account1'):")
    await state.set_state(AccountAdditionStates.WaitingForLabel)


@dp.message(AccountAdditionStates.WaitingForLabel, F.text, ~F.text.startswith('/'))
async def handle_account_add_label(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    label_or_session_name = message.text.strip()
    if not label_or_session_name:
        await message.answer("Метка/имя сессии не может быть пустой. Попробуйте снова.")
        return

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM accounts WHERE session_name = ?", (label_or_session_name,))
    if cursor.fetchone():
        conn.close()
        await message.answer(f"Аккаунт с именем сессии '{label_or_session_name}' уже существует. Введите другую метку.")
        return
    conn.close()

    await state.update_data(label=label_or_session_name)
    logger.info(f"Admin {message.from_user.id}: State after label: {await state.get_data()}")

    free_proxy = get_unassigned_proxy()
    if free_proxy:
        await message.answer(
            f"Найден свободный прокси: {free_proxy['proxy_ip']}:{free_proxy['proxy_port']}. Применяю...")
        await state.update_data(
            proxy_id=free_proxy['id'],
            proxy_type=free_proxy['proxy_type'],
            proxy_ip=free_proxy['proxy_ip'],
            proxy_port=free_proxy['proxy_port'],
            proxy_username=free_proxy.get('proxy_username'),
            proxy_password=free_proxy.get('proxy_password')
        )
    else:
        await message.answer("Свободных прокси не найдено. Аккаунт будет добавлен без прокси.")
        await state.update_data(proxy_id=None, proxy_type=None, proxy_ip=None, proxy_port=None, proxy_username=None,
                                proxy_password=None)

    await message.answer("Начинаю процесс подключения к Telegram...")
    await initiate_telegram_connection(message, state, message.from_user.id)


async def initiate_telegram_connection(message_object_for_reply: Message, state: FSMContext, current_admin_id: int):
    admin_id = current_admin_id
    chat_id_to_reply = message_object_for_reply.chat.id
    data = await state.get_data()
    logger.info(f"Admin {admin_id}: Initiate connection - FSM data: {data}")

    phone = data.get("phone")
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    label = data.get("label")

    if not all([phone, api_id, api_hash, label]):
        error_msg = f"Initiate connection - CRITICAL: Missing core account data! P:{phone}, AID:{api_id}, AH:{api_hash}, L:{label}"
        logger.error(f"Admin {admin_id}: {error_msg}")
        await bot.send_message(chat_id_to_reply,
                               f"Критическая ошибка: основные данные аккаунта отсутствуют. {error_msg}. Начните заново /start.")
        await state.clear()
        return

    proxy_details_fsm = {}
    if data.get("proxy_ip") and data.get("proxy_port"):
        proxy_details_fsm = {
            "proxy_type": (data.get("proxy_type") or 'socks5').lower(),
            "addr": data["proxy_ip"],
            "port": int(data["proxy_port"])
        }
        if data.get("proxy_username"):
            proxy_details_fsm["username"] = data["proxy_username"]
        if data.get("proxy_password"):
            proxy_details_fsm["password"] = data["proxy_password"]

    proxy_to_use = proxy_details_fsm if proxy_details_fsm else None
    logger.info(f"Admin {admin_id}: Proxy to use for TelegramClient for account {label}: {proxy_to_use}")

    client = None
    try:
        session_path = os.path.join(get_workspace_session_dir(), f"{label}.session")
        client = TelegramClient(SQLiteSession(session_path), api_id, api_hash, proxy=proxy_to_use)
        logger.info(
            f"Admin {admin_id}: TelegramClient created for {label} with SQLiteSession. Caching under admin_id {admin_id}")
        global_reg_cache[admin_id] = client

        logger.info(f"Admin {admin_id}: Attempting to connect client for {label}...")
        await client.connect()
        logger.info(f"Admin {admin_id}: Client connected for {label}. Checking authorization...")

        if await client.is_user_authorized():
            logger.warning(f"Admin {admin_id}: Account {label} is already authorized or session exists.")
            await client.disconnect()
            global_reg_cache.pop(admin_id, None)
            await bot.send_message(chat_id_to_reply,
                                   "Этот аккаунт уже авторизован или сессия с таким именем существует и активна! Проверьте метку.",
                                   reply_markup=main_menu_keyboard())
            await state.clear()
            return

        logger.info(f"Admin {admin_id}: Client for {label} not authorized. Sending code request to phone {phone}...")
        code_request = await client.send_code_request(phone)
        await state.update_data(phone_code_hash=code_request.phone_code_hash)
        logger.info(
            f"Admin {admin_id}: Code request sent for {label}. Phone code hash stored. New state: {await state.get_data()}")

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Отправить код ещё раз", callback_data="account_add_resend_code")]
        ])
        await bot.send_message(chat_id_to_reply, "Проверьте Telegram (или SMS). Введите код:", reply_markup=kb)
        await state.set_state(AccountAdditionStates.WaitingForCode)
    except (
            ApiIdInvalidError, PhoneNumberInvalidError, PhonePasswordFloodError, PhoneNumberFloodError,
            PhoneNumberBannedError,
            PhoneCodeEmptyError, PhoneCodeHashEmptyError) as tel_err:
        error_msg = f"Telegram API error for {label}: {type(tel_err).__name__} - {tel_err}"
        logger.error(f"Admin {admin_id}: {error_msg}", exc_info=True)
        await bot.send_message(chat_id_to_reply,
                               f"Ошибка Telegram при запросе кода: {type(tel_err).__name__}. Проверьте данные. {tel_err}",
                               reply_markup=main_menu_keyboard())
        if client and client.is_connected():
            logger.info(f"Admin {admin_id}: Disconnecting client for {label} due to Telegram API error.")
            await client.disconnect()
        global_reg_cache.pop(admin_id, None)
        await state.clear()
    except Exception as e:
        error_msg = f"Error during initiate_telegram_connection for {label}: {type(e).__name__} - {e}"
        logger.error(f"Admin {admin_id}: {error_msg}", exc_info=True)
        await bot.send_message(chat_id_to_reply, f"Ошибка при отправке кода/подключении: {e}",
                               reply_markup=main_menu_keyboard())
        if client and client.is_connected():
            logger.info(f"Admin {admin_id}: Disconnecting client for {label} due to error.")
            await client.disconnect()
        global_reg_cache.pop(admin_id, None)
        await state.clear()


@dp.callback_query(F.data == "account_add_resend_code", AccountAdditionStates.WaitingForCode)
async def cb_account_add_resend_code(callback: CallbackQuery, state: FSMContext):
    admin_id = callback.from_user.id
    if not user_is_allowed(admin_id): await callback.answer("Нет прав."); return
    data = await state.get_data()
    phone = data.get("phone")
    client = global_reg_cache.get(admin_id)

    logger.info(
        f"Admin {admin_id}: Resend code. FSM Data: {data}, Client from cache: {'Exists' if client else 'Not Exists'}")

    if not client or not phone:
        logger.error(
            f"Admin {admin_id}: Resend code - ERROR: Client from cache is missing (actual client: {client}) or phone from FSM is missing (actual phone: {phone}).")
        await callback.message.answer("Ошибка: не найдены данные для повторной отправки кода. Начните заново /start.",
                                      reply_markup=main_menu_keyboard())
        await state.clear()
        await callback.answer()
        return
    try:
        if not client.is_connected():
            logger.info(f"Admin {admin_id}: Resend code - Client not connected, attempting connect.")
            await client.connect()
        logger.info(f"Admin {admin_id}: Resend code - Sending code request to {phone}.")
        code_request = await client.send_code_request(phone)
        await state.update_data(phone_code_hash=code_request.phone_code_hash)
        logger.info(f"Admin {admin_id}: Resend code - Code request sent. New state: {await state.get_data()}")
        await callback.message.answer("Код запрошен повторно. Проверьте Telegram или SMS.")
    except Exception as e:
        logger.error(f"Admin {admin_id}: Error on resend code: {e}", exc_info=True)
        await callback.message.answer(f"Ошибка при повторном запросе кода: {e}")
    await callback.answer()


@dp.message(AccountAdditionStates.WaitingForCode, F.text, ~F.text.startswith('/'))
async def handle_account_add_code(message: Message, state: FSMContext):
    admin_id = message.from_user.id
    if not user_is_allowed(admin_id): return
    code = message.text.strip()
    data = await state.get_data()

    logger.info(f"Admin {admin_id}: Handle code - FSM Data: {data}")

    phone = data.get("phone")
    api_id = data.get("api_id")
    api_hash = data.get("api_hash")
    label = data.get("label")
    phone_code_hash = data.get("phone_code_hash")
    client: TelegramClient = global_reg_cache.get(admin_id)

    error_details = []
    if not phone: error_details.append("phone missing")
    if not api_id: error_details.append("api_id missing")
    if not api_hash: error_details.append("api_hash missing")
    if not label: error_details.append("label missing")
    if not phone_code_hash: error_details.append("phone_code_hash missing")
    if not client: error_details.append(f"client missing from cache for admin_id {admin_id}")

    if error_details:
        full_error_msg = f"Handle code - Validation failed for admin {admin_id}: {', '.join(error_details)}. FSM: {data}. Client in cache from global_reg_cache.get({admin_id}): {bool(client)}"
        logger.error(full_error_msg)
        await message.answer(
            f"Произошла ошибка с данными сессии. Детали: {', '.join(error_details)}. Начните добавление аккаунта заново /start.",
            reply_markup=main_menu_keyboard())
        if client and client.is_connected(): await client.disconnect()
        global_reg_cache.pop(admin_id, None)
        await state.clear()
        return

    try:
        if not client.is_connected():
            logger.info(f"Admin {admin_id}: Handle code - Client for {label} not connected, attempting connect.")
            await client.connect()
        logger.info(
            f"Admin {admin_id}: Handle code - Signing in for {label} with phone {phone}, code {code[:2]}..., hash {str(phone_code_hash)[:5]}...")
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        logger.info(f"Admin {admin_id}: Handle code - Sign in successful for {label} (before 2FA check).")
    except PhoneCodeInvalidError:
        logger.warning(f"Admin {admin_id}: Handle code - Invalid code for {label}.")
        await message.answer("Неверный код. Попробуйте снова или нажмите &laquo;Отправить код ещё раз&raquo;.")
        return
    except PhoneCodeExpiredError:
        logger.warning(f"Admin {admin_id}: Handle code - Code expired for {label}.")
        await message.answer("Код истёк. Нажмите &laquo;Отправить код ещё раз&raquo; или начните заново /start.")
        return
    except SessionPasswordNeededError:
        logger.info(f"Admin {admin_id}: Handle code - 2FA password needed for {label}.")
        await message.answer("Аккаунт защищён 2FA. Введите пароль.")
        await state.set_state(AccountAdditionStates.WaitingFor2FAPassword)
        return
    except FloodWaitError as e:
        logger.warning(f"Admin {admin_id}: Handle code - FloodWait for {label}: {e.seconds}s.")
        await message.answer(f"Слишком много попыток. Подождите {e.seconds} секунд.")
        return
    except (
            ApiIdInvalidError, PhoneNumberInvalidError, PhonePasswordFloodError, PhoneNumberFloodError,
            PhoneNumberBannedError,
            PhoneCodeEmptyError, PhoneCodeHashEmptyError) as tel_err:
        error_msg = f"Telegram API error during sign_in for {label}: {type(tel_err).__name__} - {tel_err}"
        logger.error(f"Admin {admin_id}: {error_msg}", exc_info=True)
        await message.answer(f"Ошибка Telegram при вводе кода: {type(tel_err).__name__}. Проверьте данные. {tel_err}",
                             reply_markup=main_menu_keyboard())
        if client.is_connected(): await client.disconnect()
        global_reg_cache.pop(admin_id, None)
        await state.clear()
        return
    except Exception as e:
        logger.error(f"Admin {admin_id}: Handle code - Error during sign_in for {label}: {e}", exc_info=True)
        await message.answer(f"Ошибка при авторизации: {e}", reply_markup=main_menu_keyboard())
        if client.is_connected(): await client.disconnect()
        global_reg_cache.pop(admin_id, None)
        await state.clear()
        return

    logger.info(f"Admin {admin_id}: Handle code - Finalizing account add for {label} after code (no 2FA path).")
    await finalize_account_add(client, data, message, state)


@dp.message(AccountAdditionStates.WaitingFor2FAPassword, F.text, ~F.text.startswith('/'))
async def handle_account_add_2fa_password(message: Message, state: FSMContext):
    admin_id = message.from_user.id
    if not user_is_allowed(admin_id): return
    password = message.text.strip()
    data = await state.get_data()
    label = data.get("label", "UnknownAccount")
    client: TelegramClient = global_reg_cache.get(admin_id)

    logger.info(
        f"Admin {admin_id}: Handle 2FA for {label}. FSM Data: {data}, Client from cache: {'Exists' if client else 'Not Exists'}")

    if not client:
        logger.error(f"Admin {admin_id}: Handle 2FA - ERROR: Client not found in cache for {label}.")
        await message.answer("Ошибка сессии (клиент не найден). Начните заново /start.",
                             reply_markup=main_menu_keyboard())
        await state.clear()
        return
    try:
        if not client.is_connected():
            logger.info(f"Admin {admin_id}: Handle 2FA - Client not connected for {label}, attempting connect.")
            await client.connect()
        logger.info(f"Admin {admin_id}: Handle 2FA - Signing in with 2FA password for {label}.")
        await client.sign_in(password=password)
        logger.info(f"Admin {admin_id}: Handle 2FA - Sign in with 2FA successful for {label}.")
    except SessionPasswordNeededError:
        logger.warning(f"Admin {admin_id}: Handle 2FA - Invalid 2FA password for {label}.")
        await message.answer("Неверный 2FA пароль. Попробуйте снова.")
        return
    except Exception as e:
        logger.error(f"Admin {admin_id}: Handle 2FA - Error during 2FA sign_in for {label}: {e}", exc_info=True)
        await message.answer(f"Ошибка при вводе 2FA пароля: {e}. Попробуйте снова или начните заново /start.",
                             reply_markup=main_menu_keyboard())
        if client.is_connected(): await client.disconnect()
        global_reg_cache.pop(admin_id, None)
        await state.clear()
        return

    logger.info(f"Admin {admin_id}: Handle 2FA - Finalizing account add for {label} after 2FA.")
    await finalize_account_add(client, data, message, state)


async def finalize_account_add(client: TelegramClient, acc_data_fsm: dict, message: Message, state: FSMContext):
    admin_id = message.from_user.id
    label_for_log = acc_data_fsm.get("label", "UnknownAccount")
    logger.info(f"Admin {admin_id}: Finalize - Starting for {label_for_log}. FSM data: {acc_data_fsm}")

    if not await client.is_user_authorized():
        logger.error(f"Admin {admin_id}: Finalize - Client for {label_for_log} not authorized before saving.")
        await message.answer("Не удалось авторизоваться (финальная проверка). Попробуйте заново /start.",
                             reply_markup=main_menu_keyboard())
        if client.is_connected(): await client.disconnect()
        global_reg_cache.pop(admin_id, None)
        await state.clear()
        return

    me = await client.get_me()
    user_id_val = me.id
    logger.info(f"Admin {admin_id}: Finalize - Got user_id {user_id_val} for {label_for_log}.")

    try:
        await client(
            SetPrivacyRequest(key=InputPrivacyKeyPhoneNumber(), rules=[InputPrivacyValueDisallowAll()]))
        logger.info(f"Admin {admin_id}: Successfully set phone number privacy to 'Nobody' for {label_for_log}.")
    except Exception as e_privacy:
        logger.error(f"Admin {admin_id}: Failed to set phone number privacy for {label_for_log}: {e_privacy}")
        await message.answer(
            f"⚠️ Не удалось автоматически скрыть номер телефона для аккаунта {label_for_log}. Вы можете сделать это вручную в настройках приватности Telegram.")

    phone = acc_data_fsm.get("phone")
    api_id = acc_data_fsm.get("api_id")
    api_hash = acc_data_fsm.get("api_hash")
    session_name_is_label = acc_data_fsm.get("label")

    if not all([phone, api_id, api_hash, session_name_is_label]):
        logger.error(
            f"Admin {admin_id}: Finalize - CRITICAL: Missing core data before DB insert for {label_for_log}! P:{phone} AID:{api_id} AH:{api_hash} L:{session_name_is_label}")
        await message.answer("Критическая ошибка: отсутствуют данные для сохранения аккаунта. Начните заново /start.")
        if client.is_connected(): await client.disconnect()
        global_reg_cache.pop(admin_id, None)
        await state.clear()
        return

    proxy_type = acc_data_fsm.get("proxy_type")
    proxy_ip = acc_data_fsm.get("proxy_ip")
    proxy_port = acc_data_fsm.get("proxy_port")
    proxy_username = acc_data_fsm.get("proxy_username")
    proxy_password = acc_data_fsm.get("proxy_password")
    proxy_id_to_assign = acc_data_fsm.get("proxy_id")

    if not proxy_id_to_assign:
        free_proxy = get_unassigned_proxy()
        if free_proxy:
            proxy_id_to_assign = free_proxy["id"]
            proxy_type = free_proxy.get("proxy_type")
            proxy_ip = free_proxy.get("proxy_ip")
            proxy_port = free_proxy.get("proxy_port")
            proxy_username = free_proxy.get("proxy_username")
            proxy_password = free_proxy.get("proxy_password")
            await message.answer(
                f"Найден свободный прокси перед сохранением: {proxy_ip}:{proxy_port}. Привязываю к аккаунту..."
            )

    logger.info(
        f"Admin {admin_id}: Finalize - Proxy data for DB for {label_for_log}: type={proxy_type}, ip={proxy_ip}, port={proxy_port}")

    new_account_db_id = None
    try:
        logger.info(f"Admin {admin_id}: Finalize - Inserting account {session_name_is_label} into DB.")
        new_account_db_id = create_account_with_proxy(
            {
                "session_name": session_name_is_label,
                "phone": phone,
                "api_id": api_id,
                "api_hash": api_hash,
                "label": session_name_is_label,
                "user_id": user_id_val,
                "proxy_type": proxy_type,
                "proxy_ip": proxy_ip,
                "proxy_port": proxy_port,
                "proxy_username": proxy_username,
                "proxy_password": proxy_password,
            },
            proxy_id=proxy_id_to_assign,
        )
        logger.info(
            f"Admin {admin_id}: Finalize - Account {session_name_is_label} inserted with DB ID {new_account_db_id}.")

    except sqlite3.IntegrityError as e_sql:
        logger.error(f"Admin {admin_id}: Finalize - SQLite IntegrityError for {session_name_is_label}: {e_sql}")
        await message.answer(f"Аккаунт с сессией '{session_name_is_label}' уже существует в БД. Начните заново /start.",
                             reply_markup=main_menu_keyboard())
        if client.is_connected(): await client.disconnect()
        global_reg_cache.pop(admin_id, None)
        await state.clear()
        return

    client_data_for_userbot = {
        "id": new_account_db_id,
        "session_name": session_name_is_label,
        "phone": phone,
        "api_id": api_id,
        "api_hash": api_hash,
        "label": session_name_is_label,
        "user_id": user_id_val,
        "proxy_type": proxy_type,
        "proxy_ip": proxy_ip,
        "proxy_port": proxy_port,
        "proxy_username": proxy_username,
        "proxy_password": proxy_password,
        "_workspace_id": get_active_workspace_id()
    }
    await attach_authorized_client_to_runtime(
        new_account_db_id,
        client,
        client_data_for_userbot,
        user_id=user_id_val,
        workspace_id=get_active_workspace_id()
    )
    logger.info(
        f"Admin {admin_id}: Finalize - Client for {session_name_is_label} attached to runtime.")

    await message.answer(
        f"Аккаунт {phone} (метка/сессия: {session_name_is_label}, UID: {user_id_val}) успешно добавлен и запущен!",
        reply_markup=main_menu_keyboard()
    )
    if state:
        global_reg_cache.pop(admin_id, None)
        await state.clear()
    logger.info(
        f"Admin {admin_id}: Finalize - Account addition process complete for {session_name_is_label}. Cache and state cleared.")


@dp.callback_query(F.data == "account_remove_info")
async def cb_account_remove_info(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await callback.message.edit_text("Введите ID аккаунта (из списка) для удаления.")
    await state.set_state("acc_remove_waiting_id")
    await callback.answer()


@dp.message(StateFilter("acc_remove_waiting_id"), F.text, ~F.text.startswith('/'))
async def handle_account_remove_id(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    if not message.text.strip().isdigit():
        await message.answer("ID должно быть числом.")
        return
    acc_id = int(message.text.strip())
    res = await remove_telethon_account(acc_id)
    await message.answer(res, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в настройки аккаунтов", callback_data="account_settings")]]))
    await state.clear()


@dp.callback_query(F.data == "account_list")
async def cb_account_list(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await state.clear()
    await show_accounts_list(callback.message, page=1, is_callback=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("account_list_page:"))
async def cb_account_list_page(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await state.clear()
    try:
        page = int(callback.data.split(":", 1)[1])
    except ValueError:
        page = 1
    await show_accounts_list(callback.message, page, is_callback=True)
    await callback.answer()


@dp.callback_query(F.data.startswith("manage_account:"))
async def cb_manage_single_account_menu(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    try:
        account_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ID аккаунта.", show_alert=True)
        return

    await state.update_data(current_managing_account_id=account_id)
    await state.set_state(AccountManagementStates.ShowingAccountMenu)

    acc_details = get_account_details(account_id)
    if not acc_details:
        await callback.message.edit_text("Аккаунт не найден в базе.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ К списку аккаунтов",
                                                                   callback_data="account_list")]
                                         ]))
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    text = f"Управление аккаунтом: <b>{html.escape(acc_details['label'] or acc_details['session_name'])}</b> (ID: {account_id})\n"
    text += f"Телефон: {acc_details['phone'] or '-'}\n"
    text += f"User ID: {acc_details['user_id'] or '-'}\n"
    is_enabled = acc_details.get("is_enabled", 1)
    text += f"Статус работы: {'включен' if is_enabled else 'выключен'}\n"

    proxy_text = "нет"
    if acc_details['proxy_ip'] and acc_details['proxy_port']:
        proxy_text = f"{acc_details['proxy_type']}:{acc_details['proxy_ip']}:{acc_details['proxy_port']}"
        if acc_details['proxy_username']:
            proxy_text += f" (User: {acc_details['proxy_username']})"
    text += f"Прокси: <code>{html.escape(proxy_text)}</code>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Edit profile", callback_data=f"profile_edit_menu:{account_id}")],
        [InlineKeyboardButton(
            text="⏸️ Выключить из работы" if is_enabled else "▶️ Включить в работу",
            callback_data=f"toggle_account_enabled:{account_id}"
        )],
        [InlineKeyboardButton(text="🗑️ Удалить аккаунт", callback_data=f"delete_account_confirm:{account_id}")],
        [InlineKeyboardButton(text="⚙️ Управление прокси", callback_data=f"proxy_manage_for_account:{account_id}")],
        [InlineKeyboardButton(text="⬅️ К списку аккаунтов", callback_data="account_list")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    if callback.data.startswith("manage_account:"):
        await callback.answer()


@dp.callback_query(F.data.startswith("toggle_account_enabled:"))
async def cb_toggle_account_enabled(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    try:
        account_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ID аккаунта.", show_alert=True)
        return

    acc_details = get_account_details(account_id)
    if not acc_details:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    current_enabled = int(acc_details.get("is_enabled", 1) or 0)
    new_enabled = 0 if current_enabled else 1

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE accounts SET is_enabled = ? WHERE id = ?", (new_enabled, account_id))
    conn.commit()
    conn.close()

    if new_enabled:
        await callback.answer("Запускаю аккаунт...", show_alert=False)
        ok, msg = await reinitialize_telethon_client(account_id)
        if not ok:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE accounts SET is_enabled = 0 WHERE id = ?", (account_id,))
            conn.commit()
            conn.close()
            await callback.message.answer(f"Аккаунт не удалось включить: {msg}")
        else:
            await callback.message.answer(f"Аккаунт включен: {msg}")
    else:
        await remove_client_from_runtime(account_id, acc_details.get("session_name") or str(account_id))
        await callback.answer("Аккаунт выключен.")

    await cb_manage_single_account_menu(callback, state)


@dp.callback_query(F.data.startswith("delete_account_confirm:"))
async def cb_delete_account_confirm(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    try:
        account_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ID аккаунта.", show_alert=True)
        return

    acc_details = get_account_details(account_id)
    if not acc_details:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    label = acc_details.get("label") or acc_details.get("session_name") or str(account_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Да, удалить аккаунт", callback_data=f"delete_account_execute:{account_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к аккаунту", callback_data=f"manage_account:{account_id}")]
    ])
    await callback.message.edit_text(
        f"Удалить аккаунт <b>{html.escape(label)}</b> (ID: {account_id})?\n"
        "Аккаунт будет удален из runtime, базы и файла сессии.",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("delete_account_execute:"))
async def cb_delete_account_execute(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    try:
        account_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ID аккаунта.", show_alert=True)
        return

    result = await remove_telethon_account(account_id)
    await state.clear()
    await callback.message.edit_text(
        result,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ К списку аккаунтов", callback_data="account_list")]
        ])
    )
    await callback.answer("Удалено.")


@dp.callback_query(F.data == "check_all_sessions")
async def cb_check_all_sessions(callback: CallbackQuery):
    admin_id = callback.from_user.id
    if not user_is_allowed(admin_id):
        await callback.answer("Access denied.")
        return

    logger.info(f"Admin {admin_id}: Starting account health check.")
    reply_kb_acc_settings = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="\u2B05\uFE0F \u041d\u0430\u0437\u0430\u0434 \u0432 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u043e\u0432",
            callback_data="account_settings"
        )
    ]])
    last_progress_edit = {"at": 0.0}

    async def edit_progress(progress: dict):
        total = int(progress.get("total") or 0)
        completed = int(progress.get("completed") or 0)
        stats = progress.get("stats") or {}
        current = html.escape(str(progress.get("current") or "-"))

        now = time.monotonic()
        if completed < total and now - last_progress_edit["at"] < 1.0:
            return
        last_progress_edit["at"] = now

        text = (
            "<b>Проверяю сессии аккаунтов</b>\n\n"
            f"{progress_bar(completed, total)} <b>{completed}/{total}</b>\n"
            f"Текущий: <code>{current}</code>\n\n"
            f"✅ OK: <b>{stats.get('ok', 0)}</b>\n"
            f"⚠️ Warning: <b>{stats.get('warning', 0)}</b>\n"
            f"❌ Bad: <b>{stats.get('bad', 0)}</b>\n"
            f"⏸️ Disabled: <b>{stats.get('disabled', 0)}</b>"
        )
        try:
            await callback.message.edit_text(text, parse_mode="HTML")
        except Exception as edit_err:
            logger.debug(f"Admin {admin_id}: health progress edit skipped: {edit_err}")

    await callback.answer("Начинаю проверку.")

    try:
        result = await run_account_health_check(
            progress_callback=edit_progress,
            include_write_probe=True
        )
    except Exception as check_err:
        logger.exception(f"Admin {admin_id}: account health check failed: {check_err}")
        await callback.message.edit_text(
            f"❌ Проверка сессий не завершилась: <code>{html.escape(str(check_err)[:500])}</code>",
            reply_markup=reply_kb_acc_settings
        )
        return

    total = result.get("total", 0)
    if not total:
        await callback.message.edit_text(
            "Аккаунтов в текущем пространстве пока нет.",
            reply_markup=reply_kb_acc_settings
        )
        return

    stats = result.get("stats") or {}
    header = (
        "<b>🏁 Проверка сессий завершена</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"✅ OK: <b>{stats.get('ok', 0)}</b>\n"
        f"⚠️ Warning: <b>{stats.get('warning', 0)}</b>\n"
        f"❌ Bad: <b>{stats.get('bad', 0)}</b>\n"
        f"⏸️ Disabled: <b>{stats.get('disabled', 0)}</b>\n\n"
    )
    report_lines = [
        format_account_health_result(item)
        for item in result.get("results", [])
    ]
    chunks = []
    current_chunk = header
    for line_content in report_lines:
        chunk_to_add = line_content + "\n\n"
        if len(current_chunk) + len(chunk_to_add) > 3900:
            chunks.append(current_chunk.strip())
            current_chunk = chunk_to_add
        else:
            current_chunk += chunk_to_add
    if current_chunk.strip():
        chunks.append(current_chunk.strip())

    for index, chunk in enumerate(chunks):
        is_first = index == 0
        is_last = index == len(chunks) - 1
        if is_first:
            await callback.message.edit_text(
                chunk,
                reply_markup=reply_kb_acc_settings if is_last else None,
                parse_mode="HTML"
            )
        else:
            await bot.send_message(
                callback.message.chat.id,
                chunk,
                reply_markup=reply_kb_acc_settings if is_last else None,
                parse_mode="HTML"
            )
