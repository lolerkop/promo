import asyncio
import html
import json
import logging
import os
import random
import shutil
import sqlite3
from collections import defaultdict

from aiogram import F
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import (CallbackQuery, Document, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from telethon import TelegramClient
from telethon.errors import (ApiIdInvalidError, FloodWaitError,
                             AuthKeyDuplicatedError, AuthKeyInvalidError,
                             AuthKeyUnregisteredError, PeerFloodError,
                             PhoneCodeEmptyError, PhoneCodeExpiredError,
                             PhoneCodeHashEmptyError, PhoneCodeInvalidError,
                             PhoneNumberBannedError, PhoneNumberFloodError,
                             PhoneNumberInvalidError, PhonePasswordFloodError,
                             RPCError, SessionPasswordNeededError,
                             SessionRevokedError, UserDeactivatedBanError,
                             UserDeactivatedError, UserRestrictedError)
from telethon.sessions import SQLiteSession
from telethon.tl.functions.account import SetPrivacyRequest, UpdateProfileRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest
from telethon.tl.functions.updates import GetStateRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputUserSelf
from telethon.tl.types import (InputPrivacyKeyPhoneNumber,
                               InputPrivacyValueDisallowAll)

from db import (add_proxies_bulk, assign_proxy_to_account, get_account_details,
                get_config_value, get_db_connection, get_unassigned_proxy,
                get_workspace_session_dir,
                remove_telethon_account, set_config_value,
                unassign_proxy_for_account, update_account_proxy_settings)
from userbot import (ALL_CLIENT_USER_IDS, conversation_tracker,
                     get_active_clients, reinitialize_telethon_client,
                     remove_client_from_runtime)

from ..bot_instance import TEMP_PHOTO_DIR, bot, dp, global_reg_cache
from ..keyboards import cancel_action_keyboard, main_menu_keyboard
from ..states import (AccountAdditionStates, AccountManagementStates,
                      AccountSettingsStates)
from ..utils import show_accounts_list, user_is_allowed

logger = logging.getLogger(__name__)


def _get_runtime_client_for_account(account_id: int):
    for client, client_data in get_active_clients():
        if client_data.get("id") == account_id and client.is_connected():
            return client, client_data
    return None, None


def _account_back_keyboard(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Back to account", callback_data=f"manage_account:{account_id}")]
    ])


async def _get_account_about(client: TelegramClient) -> str:
    try:
        full_user = await client(GetFullUserRequest(InputUserSelf()))
        return getattr(full_user.full_user, "about", None) or "-"
    except Exception as e:
        logger.warning(f"Failed to load account bio: {e}")
        return "-"


@dp.callback_query(F.data == "account_settings")
async def cb_account_settings(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗂️ Сканировать и импортировать сессии", callback_data="account_scan_and_import")],
        [InlineKeyboardButton(text="➕ Добавить по номеру телефона", callback_data="account_add")],
        [InlineKeyboardButton(text="🗂️ Массовое добавление прокси", callback_data="bulk_add_proxies_start")],
        [InlineKeyboardButton(text="❌ Удалить аккаунт", callback_data="account_remove_info")],
        [InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="account_list")],
        [InlineKeyboardButton(text="💬 Автоответчик ЛС", callback_data="auto_responder_menu")],
        [InlineKeyboardButton(text="🚀 Массовое обновление профилей", callback_data="mass_profile_update")],
        [InlineKeyboardButton(text="🚦 Проверить все сессии", callback_data="check_all_sessions")],
        [InlineKeyboardButton(text="⬅️ Назад в главное меню", callback_data="back_to_main_menu")]
    ])
    await callback.message.edit_text("⚙️ <b>Настройки аккаунтов</b>:", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "bulk_add_proxies_start")
async def cb_bulk_add_proxies_start(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("У вас нет прав.")
        return
    await callback.message.edit_text(
        "Отправьте .txt файл с прокси.\n"
        "Формат: `IP:PORT:USERNAME:PASSWORD` (каждый прокси на новой строке).",
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
                parts = line.split(':')
                if len(parts) == 4:
                    proxies_to_add.append({
                        'proxy_ip': parts[0],
                        'proxy_port': int(parts[1]),
                        'proxy_username': parts[2],
                        'proxy_password': parts[3],
                        'proxy_type': 'socks5'
                    })
        if not proxies_to_add:
            await message.answer("В файле не найдено прокси в корректном формате `IP:PORT:USER:PASS`.")
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
        json_files = [f for f in os.listdir(SESSIONS_IMPORT_DIR) if f.endswith('.json')]
    except FileNotFoundError:
        await bot.send_message(callback.from_user.id,
                               "⚠️ Папка `sessions_import` не найдена. Создайте ее и поместите файлы.")
        return

    if not json_files:
        await bot.send_message(callback.from_user.id,
                               "ℹ️ Новых `.json` файлов для импорта в папке `sessions_import` не найдено.")
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

            api_id = data.get("app_id")
            api_hash = data.get("app_hash")
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

    summary_message = f"🏁 Сканирование завершено!\n\n" \
                      f"✅ Успешно импортировано: {success_count}\n" \
                      f"ℹ️ Пропущено (дубликаты): {skipped_count}\n" \
                      f"❌ Ошибок: {error_count}"
    await bot.send_message(callback.from_user.id, summary_message)


@dp.callback_query(F.data == "auto_responder_menu")
async def cb_auto_responder_menu(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return

    current_message = get_config_value("dm_auto_reply_message", "Сообщение автоответчика не установлено.")
    is_enabled = get_config_value("dm_auto_reply_enabled", "False").lower() == 'true'
    status_text = "🟢 Включен" if is_enabled else "🔴 Выключен"

    text = f"<b>Автоответчик для личных сообщений</b>\n\n"
    text += f"Статус: {status_text}\n\n"
    text += f"Текущее сообщение:\n<pre>{html.escape(current_message if current_message else 'Не установлено')}</pre>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить сообщение", callback_data="set_dm_auto_reply_message")],
        [InlineKeyboardButton(text=f"{'Выключить' if is_enabled else 'Включить'}",
                              callback_data="toggle_dm_auto_reply")],
        [InlineKeyboardButton(text="⬅️ Назад в настройки аккаунтов", callback_data="account_settings")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data == "toggle_dm_auto_reply")
async def cb_toggle_dm_auto_reply(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    is_enabled = get_config_value("dm_auto_reply_enabled", "False").lower() == 'true'
    new_status_str = "False" if is_enabled else "True"
    set_config_value("dm_auto_reply_enabled", new_status_str)

    await cb_auto_responder_menu(callback, state)
    await callback.answer(f"Автоответчик {'выключен' if is_enabled else 'включен'}.")


@dp.callback_query(F.data == "set_dm_auto_reply_message")
async def cb_set_dm_auto_reply_message(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("Нет прав.")
        return
    await callback.message.edit_text("Введите новое сообщение для автоответчика:")
    await state.set_state(AccountSettingsStates.WaitingForDMAutoReplyMessage)
    await callback.answer()


@dp.message(AccountSettingsStates.WaitingForDMAutoReplyMessage, F.text, ~F.text.startswith('/'))
async def process_dm_auto_reply_message(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return

    new_reply_message = message.text.strip() if message.text else ""
    set_config_value("dm_auto_reply_message", new_reply_message)

    await message.answer(f"Сообщение автоответчика установлено:\n<pre>{html.escape(new_reply_message)}</pre>",
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                             [InlineKeyboardButton(text="⬅️ К настройкам автоответчика",
                                                   callback_data="auto_responder_menu")]
                         ]))
    await state.clear()


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
    await message.answer("Введите API_ID (число).")
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
    userbot_clients_list = get_active_clients()
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

    logger.info(
        f"Admin {admin_id}: Finalize - Proxy data for DB for {label_for_log}: type={proxy_type}, ip={proxy_ip}, port={proxy_port}")

    conn = get_db_connection()
    cursor = conn.cursor()
    new_account_db_id = None
    try:
        logger.info(f"Admin {admin_id}: Finalize - Inserting account {session_name_is_label} into DB.")
        cursor.execute(
            """INSERT INTO accounts (session_name, phone, api_id, api_hash, label, user_id,
                                    proxy_type, proxy_ip, proxy_port, proxy_username, proxy_password)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (session_name_is_label, phone, api_id, api_hash, session_name_is_label, user_id_val,
             proxy_type, proxy_ip, proxy_port, proxy_username, proxy_password)
        )
        conn.commit()
        new_account_db_id = cursor.lastrowid
        logger.info(
            f"Admin {admin_id}: Finalize - Account {session_name_is_label} inserted with DB ID {new_account_db_id}.")
        if proxy_id_to_assign and new_account_db_id:
            assign_proxy_to_account(proxy_id_to_assign, new_account_db_id)

    except sqlite3.IntegrityError as e_sql:
        conn.rollback()
        logger.error(f"Admin {admin_id}: Finalize - SQLite IntegrityError for {session_name_is_label}: {e_sql}")
        await message.answer(f"Аккаунт с сессией '{session_name_is_label}' уже существует в БД. Начните заново /start.",
                             reply_markup=main_menu_keyboard())
        if client.is_connected(): await client.disconnect()
        global_reg_cache.pop(admin_id, None)
        await state.clear()
        return
    finally:
        conn.close()

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
        "proxy_password": proxy_password
    }
    logger.info(
        f"Admin {admin_id}: Finalize - Client data for userbot list for {label_for_log}: {client_data_for_userbot}")
    conversation_tracker[new_account_db_id] = defaultdict(int)

    asyncio.create_task(client.run_until_disconnected())
    logger.info(f"Admin {admin_id}: Finalize - Client task created for {session_name_is_label}.")

    userbot_clients_list.append((client, client_data_for_userbot))
    ALL_CLIENT_USER_IDS.add(user_id_val)
    logger.info(
        f"Admin {admin_id}: Finalize - Client for {session_name_is_label} added to userbot_clients_list and ALL_CLIENT_USER_IDS.")

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


@dp.callback_query(F.data.startswith("profile_edit_menu:"))
async def cb_profile_edit_menu(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id):
        await callback.answer("No access.")
        return
    try:
        account_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Invalid account id.", show_alert=True)
        return

    client, client_data = _get_runtime_client_for_account(account_id)
    if not client:
        await callback.message.edit_text(
            "This account is not connected. Restart/check the session first.",
            reply_markup=_account_back_keyboard(account_id)
        )
        await callback.answer()
        return

    me = await client.get_me()
    display_name = " ".join(filter(None, [me.first_name, me.last_name])) or client_data.get("label", str(account_id))
    about = await _get_account_about(client)
    await state.update_data(current_managing_account_id=account_id)

    text = (
        f"Edit profile for <b>{html.escape(display_name)}</b> (ID: {account_id})\n"
        f"Username: @{html.escape(me.username) if me.username else '-'}\n"
        f"Bio: <pre>{html.escape(about)}</pre>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="First name", callback_data=f"profile_set_first:{account_id}")],
        [InlineKeyboardButton(text="Last name", callback_data=f"profile_set_last:{account_id}")],
        [InlineKeyboardButton(text="Bio / description", callback_data=f"profile_set_bio:{account_id}")],
        [InlineKeyboardButton(text="Avatar", callback_data=f"profile_set_photo:{account_id}")],
        [InlineKeyboardButton(text="Back to account", callback_data=f"manage_account:{account_id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("profile_set_first:"))
async def cb_profile_set_first(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("No access."); return
    account_id = int(callback.data.split(":")[1])
    await state.update_data(current_managing_account_id=account_id)
    await callback.message.edit_text("Send new first name.", reply_markup=_account_back_keyboard(account_id))
    await state.set_state(AccountManagementStates.WaitingForProfileFirstName)
    await callback.answer()


@dp.message(AccountManagementStates.WaitingForProfileFirstName, F.text, ~F.text.startswith('/'))
async def process_profile_first_name(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    data = await state.get_data()
    account_id = data.get("current_managing_account_id")
    client, _ = _get_runtime_client_for_account(account_id)
    if not client:
        await message.answer("Account is not connected.", reply_markup=_account_back_keyboard(account_id))
        await state.clear()
        return
    try:
        await client(UpdateProfileRequest(first_name=message.text.strip()))
        await message.answer("First name updated.", reply_markup=_account_back_keyboard(account_id))
    except Exception as e:
        await message.answer(f"Failed to update first name: {e}", reply_markup=_account_back_keyboard(account_id))
    await state.clear()


@dp.callback_query(F.data.startswith("profile_set_last:"))
async def cb_profile_set_last(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("No access."); return
    account_id = int(callback.data.split(":")[1])
    await state.update_data(current_managing_account_id=account_id)
    await callback.message.edit_text("Send new last name. Send '-' to clear it.", reply_markup=_account_back_keyboard(account_id))
    await state.set_state(AccountManagementStates.WaitingForProfileLastName)
    await callback.answer()


@dp.message(AccountManagementStates.WaitingForProfileLastName, F.text, ~F.text.startswith('/'))
async def process_profile_last_name(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    data = await state.get_data()
    account_id = data.get("current_managing_account_id")
    client, _ = _get_runtime_client_for_account(account_id)
    if not client:
        await message.answer("Account is not connected.", reply_markup=_account_back_keyboard(account_id))
        await state.clear()
        return
    last_name = "" if message.text.strip() == "-" else message.text.strip()
    try:
        await client(UpdateProfileRequest(last_name=last_name))
        await message.answer("Last name updated.", reply_markup=_account_back_keyboard(account_id))
    except Exception as e:
        await message.answer(f"Failed to update last name: {e}", reply_markup=_account_back_keyboard(account_id))
    await state.clear()


@dp.callback_query(F.data.startswith("profile_set_bio:"))
async def cb_profile_set_bio(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("No access."); return
    account_id = int(callback.data.split(":")[1])
    await state.update_data(current_managing_account_id=account_id)
    await callback.message.edit_text("Send new bio/description. Send '-' to clear it.", reply_markup=_account_back_keyboard(account_id))
    await state.set_state(AccountManagementStates.WaitingForProfileBio)
    await callback.answer()


@dp.message(AccountManagementStates.WaitingForProfileBio, F.text, ~F.text.startswith('/'))
async def process_profile_bio(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    data = await state.get_data()
    account_id = data.get("current_managing_account_id")
    client, _ = _get_runtime_client_for_account(account_id)
    if not client:
        await message.answer("Account is not connected.", reply_markup=_account_back_keyboard(account_id))
        await state.clear()
        return
    bio = "" if message.text.strip() == "-" else message.text.strip()
    if len(bio) > 70:
        await message.answer("Bio is too long. Telegram user bio is usually limited to 70 characters.",
                             reply_markup=_account_back_keyboard(account_id))
        return
    try:
        await client(UpdateProfileRequest(about=bio))
        await message.answer("Bio updated.", reply_markup=_account_back_keyboard(account_id))
    except Exception as e:
        await message.answer(f"Failed to update bio: {e}", reply_markup=_account_back_keyboard(account_id))
    await state.clear()


@dp.callback_query(F.data.startswith("profile_set_photo:"))
async def cb_profile_set_photo(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("No access."); return
    account_id = int(callback.data.split(":")[1])
    await state.update_data(current_managing_account_id=account_id)
    await callback.message.edit_text("Send a photo for the new avatar.", reply_markup=_account_back_keyboard(account_id))
    await state.set_state(AccountManagementStates.WaitingForProfilePhoto)
    await callback.answer()


@dp.message(AccountManagementStates.WaitingForProfilePhoto, F.photo)
async def process_profile_photo(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    data = await state.get_data()
    account_id = data.get("current_managing_account_id")
    client, _ = _get_runtime_client_for_account(account_id)
    if not client:
        await message.answer("Account is not connected.", reply_markup=_account_back_keyboard(account_id))
        await state.clear()
        return

    os.makedirs(TEMP_PHOTO_DIR, exist_ok=True)
    photo_path = os.path.join(TEMP_PHOTO_DIR, f"single_profile_{account_id}_{message.message_id}.jpg")
    try:
        await bot.download(message.photo[-1], destination=photo_path)
        uploaded_file = await client.upload_file(photo_path)
        await client(UploadProfilePhotoRequest(file=uploaded_file))
        await message.answer("Avatar updated.", reply_markup=_account_back_keyboard(account_id))
    except Exception as e:
        await message.answer(f"Failed to update avatar: {e}", reply_markup=_account_back_keyboard(account_id))
    finally:
        if os.path.exists(photo_path):
            try:
                os.remove(photo_path)
            except Exception:
                pass
        await state.clear()


@dp.message(AccountManagementStates.WaitingForProfilePhoto, ~F.text.startswith('/'))
async def process_profile_photo_invalid(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    data = await state.get_data()
    account_id = data.get("current_managing_account_id")
    await message.answer("Please send an image/photo.", reply_markup=_account_back_keyboard(account_id))


@dp.callback_query(F.data.startswith("proxy_manage_for_account:"))
async def cb_manage_proxy_for_account(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    try:
        account_id = int(callback.data.split(":")[1])
    except (IndexError, ValueError):
        await callback.answer("Неверный ID аккаунта.", show_alert=True)
        return

    await state.update_data(current_managing_account_id=account_id)
    acc_details = get_account_details(account_id)

    if not acc_details:
        await callback.message.edit_text("Аккаунт не найден.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ К списку аккаунтов",
                                                                   callback_data="account_list")]
                                         ]))
        return

    text = f"Управление прокси для аккаунта: <b>{html.escape(acc_details['label'] or acc_details['session_name'])}</b> (ID: {account_id})\n\n"
    current_proxy_text = "Прокси не установлен."
    if acc_details['proxy_ip'] and acc_details['proxy_port']:
        current_proxy_text = f"Тип: {acc_details['proxy_type'] or 'socks5'}\n" \
                             f"IP: {acc_details['proxy_ip']}\n" \
                             f"Порт: {acc_details['proxy_port']}\n" \
                             f"Логин: {acc_details['proxy_username'] or '-'}\n" \
                             f"Пароль: {'********' if acc_details['proxy_password'] else '-'}"
    text += f"Текущие настройки:\n<pre>{html.escape(current_proxy_text)}</pre>"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить/Добавить прокси", callback_data=f"edit_proxy_start:{account_id}")],
        [InlineKeyboardButton(text="🗑️ Удалить прокси", callback_data=f"delete_proxy_confirm:{account_id}")],
        [InlineKeyboardButton(text="🚦 Перезапустить с текущим прокси", callback_data=f"check_proxy:{account_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к аккаунту", callback_data=f"manage_account:{account_id}")]
    ])
    await callback.message.edit_text(text, reply_markup=kb)
    await state.set_state(AccountManagementStates.ManagingProxy)
    await callback.answer()


@dp.callback_query(F.data.startswith("delete_proxy_confirm:"), AccountManagementStates.ManagingProxy)
async def cb_delete_proxy_confirm(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    account_id = int(callback.data.split(":")[1])

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔴 Да, удалить прокси", callback_data=f"execute_delete_proxy:{account_id}")],
        [InlineKeyboardButton(text="Отмена", callback_data=f"proxy_manage_for_account:{account_id}")]
    ])
    await callback.message.edit_text(
        f"Вы уверены, что хотите удалить прокси для аккаунта ID {account_id}?\n"
        "Аккаунт будет перезапущен без прокси.", reply_markup=kb)
    await callback.answer()


@dp.callback_query(F.data.startswith("execute_delete_proxy:"), AccountManagementStates.ManagingProxy)
async def cb_execute_delete_proxy(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    account_id = int(callback.data.split(":")[1])

    unassign_proxy_for_account(account_id)

    proxy_details_to_set = {
        'proxy_type': None, 'proxy_ip': None, 'proxy_port': None,
        'proxy_username': None, 'proxy_password': None
    }
    update_success = update_account_proxy_settings(account_id, proxy_details_to_set)

    if update_success:
        await callback.message.edit_text(
            f"Прокси для аккаунта ID {account_id} удален из БД. Перезапускаю клиент...")
        reinit_success, reinit_message = await reinitialize_telethon_client(account_id)
        if reinit_success:
            await callback.message.answer(f"✅ {reinit_message}",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                              [InlineKeyboardButton(text="⬅️ К управлению прокси",
                                                                    callback_data=f"proxy_manage_for_account:{account_id}")]
                                          ]))
        else:
            await callback.message.answer(f"⚠️ {reinit_message}",
                                          reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                              [InlineKeyboardButton(text="⬅️ К управлению прокси",
                                                                    callback_data=f"proxy_manage_for_account:{account_id}")]
                                          ]))
    else:
        await callback.message.edit_text("Не удалось удалить прокси из БД.",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ К управлению прокси",
                                                                   callback_data=f"proxy_manage_for_account:{account_id}")]
                                         ]))
    await callback.answer()
    await state.set_state(AccountManagementStates.ShowingAccountMenu)


@dp.callback_query(F.data.startswith("check_proxy:"), AccountManagementStates.ManagingProxy)
async def cb_check_proxy(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    account_id = int(callback.data.split(":")[1])

    await callback.message.edit_text(
        f"Начинаю проверку/перезапуск клиента ID {account_id} с текущими настройками прокси...")
    reinit_success, reinit_message = await reinitialize_telethon_client(account_id)

    if reinit_success:
        await callback.message.edit_text(f"✅ Проверка/перезапуск успешна: {reinit_message}",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ К управлению прокси",
                                                                   callback_data=f"proxy_manage_for_account:{account_id}")]
                                         ]))
    else:
        await callback.message.edit_text(f"⚠️ Проверка/перезапуск не удалась: {reinit_message}",
                                         reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                             [InlineKeyboardButton(text="⬅️ К управлению прокси",
                                                                   callback_data=f"proxy_manage_for_account:{account_id}")]
                                         ]))
    await callback.answer()
    await state.set_state(AccountManagementStates.ShowingAccountMenu)


@dp.callback_query(F.data.startswith("edit_proxy_start:"), AccountManagementStates.ManagingProxy)
async def cb_edit_proxy_start(callback: CallbackQuery, state: FSMContext):
    if not user_is_allowed(callback.from_user.id): await callback.answer("Нет прав."); return
    account_id = int(callback.data.split(":")[1])
    await state.update_data(current_managing_account_id=account_id)

    await callback.message.edit_text(
        f"Изменение прокси для аккаунта ID {account_id}.\n"
        "Введите тип прокси (socks5, socks4, http). \nОтправьте /skip или 'пропустить', если не знаете (будет socks5).")
    await state.set_state(AccountManagementStates.WaitingForProxyTypeUpdate)
    await callback.answer()


@dp.message(AccountManagementStates.WaitingForProxyTypeUpdate, F.text, ~F.text.startswith('/'))
async def process_proxy_type_update(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    proxy_type_input = message.text.strip().lower()

    if proxy_type_input in ["/skip", "пропустить"]:
        await state.update_data(proxy_type_update='socks5')
        await message.answer("Тип прокси 'socks5'. Введите IP-адрес прокси:")
    elif proxy_type_input in ['socks5', 'socks4', 'http']:
        await state.update_data(proxy_type_update=proxy_type_input)
        await message.answer(f"Тип прокси '{proxy_type_input}'. Введите IP-адрес прокси:")
    else:
        await message.answer("Неверный тип. Допустимые: socks5, socks4, http. Или /skip, 'пропустить'.")
        return
    await state.set_state(AccountManagementStates.WaitingForProxyIPUpdate)


@dp.message(AccountManagementStates.WaitingForProxyIPUpdate, F.text, ~F.text.startswith('/'))
async def process_proxy_ip_update(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    await state.update_data(proxy_ip_update=message.text.strip())
    await message.answer("Введите порт прокси (число):")
    await state.set_state(AccountManagementStates.WaitingForProxyPortUpdate)


@dp.message(AccountManagementStates.WaitingForProxyPortUpdate, F.text, ~F.text.startswith('/'))
async def process_proxy_port_update(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    if not message.text.strip().isdigit():
        await message.answer("Порт должен быть числом. Попробуйте снова.")
        return
    await state.update_data(proxy_port_update=int(message.text.strip()))
    await message.answer("Введите имя пользователя для прокси (отправьте /skip или 'пропустить', если его нет):")
    await state.set_state(AccountManagementStates.WaitingForProxyUsernameUpdate)


@dp.message(AccountManagementStates.WaitingForProxyUsernameUpdate, F.text, ~F.text.startswith('/'))
async def process_proxy_username_update(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    username_input = message.text.strip()
    if username_input.lower() in ["/skip", "пропустить"]:
        await state.update_data(proxy_username_update=None)
    else:
        await state.update_data(proxy_username_update=username_input)
    await message.answer("Введите пароль для прокси (отправьте /skip или 'пропустить', если его нет):")
    await state.set_state(AccountManagementStates.WaitingForProxyPasswordUpdate)


@dp.message(AccountManagementStates.WaitingForProxyPasswordUpdate, F.text, ~F.text.startswith('/'))
async def process_proxy_password_update(message: Message, state: FSMContext):
    if not user_is_allowed(message.from_user.id): return
    password_input = message.text.strip()
    if password_input.lower() in ["/skip", "пропустить"]:
        await state.update_data(proxy_password_update=None)
    else:
        await state.update_data(proxy_password_update=password_input)

    fsm_data = await state.get_data()
    account_id = fsm_data.get("current_managing_account_id")
    if not account_id:
        await message.answer("Ошибка: ID аккаунта не найден в состоянии. Попробуйте снова.",
                             reply_markup=main_menu_keyboard())
        await state.clear()
        return

    unassign_proxy_for_account(account_id)

    proxy_details_to_set = {
        'proxy_type': fsm_data.get('proxy_type_update'),
        'proxy_ip': fsm_data.get('proxy_ip_update'),
        'proxy_port': fsm_data.get('proxy_port_update'),
        'proxy_username': fsm_data.get('proxy_username_update'),
        'proxy_password': fsm_data.get('proxy_password_update')
    }

    update_success = update_account_proxy_settings(account_id, proxy_details_to_set)

    if update_success:
        await message.answer(f"Настройки прокси для аккаунта ID {account_id} обновлены в БД. Перезапускаю клиент...")
        reinit_success, reinit_message = await reinitialize_telethon_client(account_id)
        if reinit_success:
            await message.answer(f"✅ {reinit_message}")
        else:
            await message.answer(f"⚠️ {reinit_message}")
    else:
        await message.answer(f"Не удалось обновить настройки прокси для аккаунта ID {account_id} в БД.")

    await state.set_state(AccountManagementStates.ShowingAccountMenu)
    mock_callback_message = await message.answer("Загрузка меню...")
    mock_callback = CallbackQuery(id=str(random.randint(1, 100000)), from_user=message.from_user,
                                  chat_instance=message.chat.id, message=mock_callback_message,
                                  data=f"proxy_manage_for_account:{account_id}")
    await cb_manage_proxy_for_account(mock_callback, state)
    try:
        await bot.delete_message(chat_id=mock_callback_message.chat.id, message_id=mock_callback_message.message_id)
    except:
        pass


@dp.callback_query(F.data == "check_all_sessions")
async def cb_check_all_sessions(callback: CallbackQuery):
    admin_id = callback.from_user.id
    if not user_is_allowed(admin_id):
        await callback.answer("Access denied.")
        return

    logger.info(f"Admin {admin_id}: Starting enhanced check_all_sessions.")
    await callback.message.edit_text("Checking all account sessions. This may take a few minutes...")
    await callback.answer()

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, session_name, phone, api_id, api_hash, label, user_id,
               proxy_type, proxy_ip, proxy_port, proxy_username, proxy_password
        FROM accounts
        ORDER BY id
        """
    )
    account_rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    chat_id_for_updates = callback.message.chat.id
    original_message_id_store = {"id": callback.message.message_id}
    reply_kb_acc_settings = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="\u2B05\uFE0F \u041d\u0430\u0437\u0430\u0434 \u0432 \u043d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0438 \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u043e\u0432",
            callback_data="account_settings"
        )
    ]])

    if not account_rows:
        await bot.send_message(
            chat_id_for_updates,
            "No accounts found in database.",
            reply_markup=reply_kb_acc_settings
        )
        return

    active_clients_by_id = {
        data.get("id"): (client, data)
        for client, data in get_active_clients()
        if data and data.get("id") is not None
    }

    total_clients = len(account_rows)
    results = []
    summary = {"ok": 0, "warning": 0, "bad": 0}

    async def update_progress_message(text_to_set: str):
        try:
            await bot.edit_message_text(
                text_to_set,
                chat_id=chat_id_for_updates,
                message_id=original_message_id_store["id"]
            )
        except Exception:
            try:
                temp_msg = await bot.send_message(chat_id_for_updates, text_to_set)
                original_message_id_store["id"] = temp_msg.message_id
            except Exception as send_err:
                logger.error(f"Admin {admin_id}: Failed to send/edit progress for check_all_sessions: {send_err}")

    def build_proxy_params(account_data: dict):
        if not account_data.get("proxy_ip") or not account_data.get("proxy_port"):
            return None
        proxy = {
            "proxy_type": (account_data.get("proxy_type") or "socks5").lower(),
            "addr": account_data.get("proxy_ip"),
            "port": int(account_data.get("proxy_port")),
        }
        if account_data.get("proxy_username"):
            proxy["username"] = account_data.get("proxy_username")
        if account_data.get("proxy_password"):
            proxy["password"] = account_data.get("proxy_password")
        return proxy

    async def run_health_probe(client: TelegramClient, account_data: dict):
        warnings = []
        details = []

        if not client.is_connected():
            await client.connect()
        if not client.is_connected():
            return "bad", "Could not connect to Telegram."

        if not await client.is_user_authorized():
            return "bad", "Session is not authorized or was revoked."

        me = await client.get_me()
        if not me:
            return "bad", "Authorized, but get_me returned no user data."

        full_name = f"{getattr(me, 'first_name', '') or ''} {getattr(me, 'last_name', '') or ''}".strip()
        details.append(f"Telegram ID: <code>{me.id}</code>")
        if full_name:
            details.append(f"Name: <code>{html.escape(full_name)}</code>")
        username = getattr(me, "username", None)
        if username:
            details.append(f"Username: @{html.escape(username)}")

        if getattr(me, "deleted", False):
            return "bad", "Account user object is marked as deleted/deactivated."
        if getattr(me, "restricted", False):
            warnings.append("User object has restricted=True.")
        restriction_reason = getattr(me, "restriction_reason", None)
        if restriction_reason:
            warnings.append(f"Restriction reason present: {html.escape(str(restriction_reason)[:250])}")
        if getattr(me, "scam", False):
            warnings.append("Telegram marks this account as scam.")
        if getattr(me, "fake", False):
            warnings.append("Telegram marks this account as fake.")

        await client(GetStateRequest())
        await client(GetFullUserRequest(InputUserSelf()))

        try:
            probe_message = await client.send_message(
                "me",
                "session health check",
                silent=True,
                link_preview=False
            )
            try:
                await client.delete_messages("me", [probe_message.id])
            except Exception as delete_err:
                warnings.append(f"Self-test message sent, but delete failed: {type(delete_err).__name__}.")
            details.append("Self-send probe: OK")
        except (UserRestrictedError, PeerFloodError) as e_write:
            warnings.append(f"Write probe failed: {type(e_write).__name__}. Possible freeze/restriction.")
        except RPCError as e_write_rpc:
            warnings.append(f"Write probe RPC error: {type(e_write_rpc).__name__}.")

        db_user_id = account_data.get("user_id")
        db_id = account_data.get("id")
        if db_user_id != me.id:
            details.append(f"DB UID mismatch: {html.escape(str(db_user_id))} -> {me.id}. Updated.")
            account_data["user_id"] = me.id
            conn_uid_update = get_db_connection()
            cursor_uid_update = conn_uid_update.cursor()
            try:
                cursor_uid_update.execute("UPDATE accounts SET user_id = ? WHERE id = ?", (me.id, db_id))
                conn_uid_update.commit()
            finally:
                conn_uid_update.close()

        if warnings:
            return "warning", "\n".join(details + ["Warnings:"] + [f"- {w}" for w in warnings])
        return "ok", "\n".join(details)

    fatal_session_errors = (
        AuthKeyDuplicatedError,
        AuthKeyInvalidError,
        AuthKeyUnregisteredError,
        PhoneNumberBannedError,
        SessionRevokedError,
        UserDeactivatedBanError,
        UserDeactivatedError,
    )

    for index, account_data in enumerate(account_rows, start=1):
        db_id = account_data.get("id")
        s_name = account_data.get("label") or account_data.get("session_name") or f"ID {db_id}"
        await update_progress_message(f"Checking session {index}/{total_clients}: {html.escape(str(s_name))}...")

        proxy_info = "none"
        if account_data.get("proxy_ip") and account_data.get("proxy_port"):
            proxy_info = f"{account_data.get('proxy_type') or 'socks5'}:{account_data.get('proxy_ip')}:{account_data.get('proxy_port')}"

        runtime_pair = active_clients_by_id.get(db_id)
        temp_client = None
        client = None
        source = "runtime"
        status_kind = "bad"
        status_details = "Not checked."

        try:
            if runtime_pair:
                client = runtime_pair[0]
            else:
                source = "temporary"
                session_name = account_data.get("session_name")
                if not session_name or not account_data.get("api_id") or not account_data.get("api_hash"):
                    raise ValueError("Missing session_name/api_id/api_hash in DB.")
                temp_client = TelegramClient(
                    SQLiteSession(os.path.join(get_workspace_session_dir(), session_name)),
                    int(account_data.get("api_id")),
                    account_data.get("api_hash"),
                    proxy=build_proxy_params(account_data)
                )
                client = temp_client

            status_kind, status_details = await run_health_probe(client, account_data)

        except fatal_session_errors as e_fatal:
            status_kind = "bad"
            status_details = f"Fatal session/account error: {type(e_fatal).__name__}. Account is likely banned, deactivated, or session was revoked."
            logger.warning(f"Admin {admin_id}: fatal session check error for {s_name}: {e_fatal}")
        except FloodWaitError as e_flood:
            status_kind = "warning"
            status_details = f"FloodWait during health check: {e_flood.seconds}s. Account works, but Telegram rate-limited the check."
            logger.warning(f"Admin {admin_id}: FloodWait checking {s_name}: {e_flood.seconds}s")
        except RPCError as e_rpc:
            status_kind = "warning"
            status_details = f"RPC error during check: {type(e_rpc).__name__}. Details: {html.escape(str(e_rpc)[:250])}"
            logger.warning(f"Admin {admin_id}: RPC session check error for {s_name}: {e_rpc}")
        except Exception as e:
            status_kind = "bad"
            status_details = f"Check failed: {type(e).__name__}: {html.escape(str(e)[:250])}"
            logger.error(f"Admin {admin_id}: session check failed for {s_name}: {e}", exc_info=False)
        finally:
            if temp_client and temp_client.is_connected():
                try:
                    await temp_client.disconnect()
                except Exception:
                    pass

        summary[status_kind] += 1
        icon = {"ok": "\u2705", "warning": "\u26A0\uFE0F", "bad": "\u274C"}.get(status_kind, "?")
        status_label = {"ok": "OK", "warning": "WARNING", "bad": "BAD"}.get(status_kind, status_kind.upper())
        runtime_text = "active runtime client" if source == "runtime" else "temporary DB session check"
        results.append(
            f"{icon} <b>{html.escape(str(s_name))}</b> | DB ID: <code>{db_id}</code>\n"
            f"Status: <b>{status_label}</b>\n"
            f"Source: {runtime_text}\n"
            f"Proxy: <code>{html.escape(proxy_info)}</code>\n"
            f"Details:\n{status_details}"
        )
        await asyncio.sleep(random.uniform(0.2, 0.5))

    try:
        await bot.delete_message(chat_id=chat_id_for_updates, message_id=original_message_id_store["id"])
    except Exception:
        pass

    final_report_header = (
        "<b>\U0001F3C1 Enhanced session check results</b>\n\n"
        f"Total: {total_clients}\n"
        f"\u2705 OK: {summary['ok']}\n"
        f"\u26A0\uFE0F Warnings: {summary['warning']}\n"
        f"\u274C Bad: {summary['bad']}\n\n"
    )

    current_chunk = final_report_header
    first_chunk_sent = False
    for line_content in results:
        chunk_to_add = line_content + "\n\n"
        if len(current_chunk) + len(chunk_to_add) > 3900:
            await bot.send_message(chat_id_for_updates, current_chunk.strip(), parse_mode="HTML")
            first_chunk_sent = True
            current_chunk = chunk_to_add
        else:
            current_chunk += chunk_to_add

    if current_chunk.strip():
        await bot.send_message(
            chat_id_for_updates,
            current_chunk.strip(),
            reply_markup=reply_kb_acc_settings,
            parse_mode="HTML"
        )
