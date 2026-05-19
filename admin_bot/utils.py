import asyncio
import logging
import os
import random
import time
import sqlite3
import string
import html
import uuid

from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient
from telethon.errors import (
    InviteHashExpiredError, InviteHashInvalidError, FloodWaitError,
    UserAlreadyParticipantError, ChatAdminRequiredError, RPCError, ChannelPrivateError, UserNotParticipantError,
    UsernameOccupiedError, UsernameInvalidError, UsernameNotModifiedError, UserIsBlockedError, PeerIdInvalidError,
    UserDeactivatedBanError, AuthKeyUnregisteredError, UserDeactivatedError
)
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.functions.channels import JoinChannelRequest, LeaveChannelRequest, GetFullChannelRequest
from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
from telethon.tl.functions.photos import UploadProfilePhotoRequest
from telethon.utils import get_peer_id

from shared import active_background_tasks
from db import get_db_connection, get_config_value, add_analytics_log, set_entity_assigned_account, \
    get_active_workspace_id, get_current_workspace_id, remove_telethon_account, run_in_workspace
from userbot import generate_vpn_comment, get_next_account_in_cycle, get_active_clients

from .bot_instance import bot, allowed_ids, TEMP_PHOTO_DIR

try:
    from admin_bot.reporting_utils import send_report
except ImportError:
    async def send_report(*args, **kwargs):
        logging.error("CRITICAL: send_report could not be imported in admin_bot.utils")

FATAL_ACCOUNT_ERRORS = (
    UserDeactivatedBanError,
    AuthKeyUnregisteredError,
    UserDeactivatedError
)

channel_join_rate_state = {}
subscription_runtime_loads: dict[tuple[str, int], int] = {}


def _subscription_runtime_key(account_id: int):
    return (get_current_workspace_id(), account_id)


def _progress_bar(done: int, total: int, width: int = 18) -> str:
    if total <= 0:
        return "\u2591" * width
    ratio = max(0.0, min(1.0, done / total))
    filled = int(round(ratio * width))
    return "\u2588" * filled + "\u2591" * (width - filled)


def _format_bulk_subscription_progress(
        title: str,
        task_id: str,
        total: int,
        processed: int,
        stats: dict,
        current_item: str = "",
        status: str = "",
        account_state: dict | None = None
) -> str:
    percent = int((processed / total) * 100) if total else 0
    current_item = html.escape(str(current_item)[:180]) if current_item else "-"
    status = html.escape(str(status)) if status else "Preparing..."

    lines = [
        f"<b>{html.escape(title)}</b>",
        f"<code>{task_id}</code>",
        "",
        f"<code>{_progress_bar(processed, total)}</code> {percent}%",
        f"<b>Progress:</b> {processed}/{total}",
        "",
        f"<b>Current:</b> <code>{current_item}</code>",
        f"<b>Status:</b> {status}",
        "",
        f"<b>Added to DB:</b> {stats.get('db_added', 0)}",
        f"<b>DB/info errors:</b> {stats.get('db_failed', 0)}",
        f"<b>Join OK:</b> {stats.get('success', 0)}",
        f"<b>Join failed:</b> {stats.get('failed', 0)}",
        f"<b>Accounts removed:</b> {stats.get('deleted', 0)}",
    ]

    if account_state:
        lines.append("")
        lines.append("<b>Accounts:</b>")
        for account_id, state in list(account_state.items())[:8]:
            available_at = state.get("available_at", 0) or 0
            cooldown_left = max(0, int(available_at - time.time()))
            cooldown_text = f", rest {cooldown_left // 60}m {cooldown_left % 60}s" if cooldown_left else ""
            lines.append(
                f"- ID {account_id}: ok {state.get('success', 0)}, "
                f"fail {state.get('failed', 0)}{cooldown_text}"
            )

    return "\n".join(lines)


def _get_int_config_value(key: str, default: int, min_value: int = 1) -> int:
    try:
        value = int(get_config_value(key, str(default)))
        return max(value, min_value)
    except (TypeError, ValueError):
        return default


async def _wait_for_channel_join_slot(client_data: dict, admin_chat_id: int):
    account_key = client_data.get("id") or client_data.get("session_name") or client_data.get("label")
    label = client_data.get("label") or client_data.get("session_name") or str(account_key)
    if not account_key:
        return

    batch_size = _get_int_config_value("channel_join_batch_size", 5, 1)
    cooldown_min = _get_int_config_value("channel_join_cooldown_min_seconds", 300, 1)
    cooldown_max = _get_int_config_value("channel_join_cooldown_max_seconds", 600, cooldown_min)
    if cooldown_max < cooldown_min:
        cooldown_max = cooldown_min

    state = channel_join_rate_state.setdefault(account_key, {"attempts": 0, "cooldown_until": 0.0})
    now = time.monotonic()

    if state["cooldown_until"] > now:
        sleep_for = int(state["cooldown_until"] - now)
        logging.info(f"CHANNEL_JOIN_LIMIT: account {label} is cooling down for {sleep_for}s.")
        if sleep_for >= 30:
            try:
                await bot.send_message(
                    admin_chat_id,
                    f"Join limiter: account {html.escape(str(label))} is resting for {sleep_for // 60}m {sleep_for % 60}s."
                )
            except Exception:
                pass
        await asyncio.sleep(sleep_for)
        state["attempts"] = 0
        state["cooldown_until"] = 0.0

    if state["attempts"] >= batch_size:
        cooldown = random.randint(cooldown_min, cooldown_max)
        state["cooldown_until"] = time.monotonic() + cooldown
        logging.info(
            f"CHANNEL_JOIN_LIMIT: account {label} reached {batch_size} channel join attempts. Resting {cooldown}s.")
        try:
            await bot.send_message(
                admin_chat_id,
                f"Join limiter: account {html.escape(str(label))} reached {batch_size} channel joins/attempts. Resting for {cooldown // 60}m {cooldown % 60}s."
            )
        except Exception:
            pass
        await asyncio.sleep(cooldown)
        state["attempts"] = 0
        state["cooldown_until"] = 0.0

    state["attempts"] += 1


def user_is_allowed(user_id: int) -> bool:
    return user_id in allowed_ids


async def handle_banned_account(client_data: dict, reason_error: Exception, admin_chat_id: int) -> bool:
    account_id = client_data.get("id")
    label = client_data.get("label", f"ID: {account_id}")
    logging.warning(
        f"Аккаунт '{label}' помечен как забаненный/замороженный. Причина: {type(reason_error).__name__}. Начинаю удаление."
    )

    try:
        await bot.send_message(
            admin_chat_id,
            f"‼️ <b>Автоматическое удаление неактивного аккаунта!</b>\n"
            f"Аккаунт: <code>{html.escape(str(label))}</code> (ID: {account_id})\n"
            f"Причина: <code>{type(reason_error).__name__}</code>"
        )
    except Exception as e:
        logging.error(f"Не удалось отправить начальное уведомление о бане для '{label}': {e}")

    try:
        result_msg = await remove_telethon_account(account_id)
        await asyncio.sleep(1)
        await bot.send_message(admin_chat_id, f"✅ Результат автоматического удаления:\n{result_msg}")
        return True
    except Exception as e:
        logging.error(f"Критическая ошибка при автоматическом удалении аккаунта '{label}': {e}", exc_info=True)
        try:
            await bot.send_message(admin_chat_id, f"💥 Не удалось автоматически удалить аккаунт '{label}': {e}")
        except Exception as final_e:
            logging.error(f"Не удалось даже отправить сообщение о критической ошибке удаления: {final_e}")
        return False


async def send_ai_welcome_message_to_chat(admin_chat_id: int, target_chat_id: int, target_chat_name: str,
                                          entity_type: str):
    if get_config_value("welcome_message_enabled", "False").lower() != 'true':
        logging.info(f"Приветственные сообщения отключены. Пропуск для {entity_type} '{target_chat_name}'.")
        return
    try:
        userbot_clients_list = get_active_clients()
        delay_seconds = random.randint(60, 600)
        logging.info(
            f"Приветственное сообщение для {entity_type} '{target_chat_name}' (ID: {target_chat_id}) будет отправлено через {delay_seconds} сек.")
        await asyncio.sleep(delay_seconds)

        active_clients_data = [cd for c, cd in userbot_clients_list if c.is_connected()]
        if not active_clients_data:
            logging.warning(f"Нет активных клиентов для отправки приветственного сообщения в {target_chat_name}.")
            await bot.send_message(admin_chat_id,
                                   f"⚠️ Не удалось отправить приветствие в {entity_type} '{html.escape(target_chat_name)}': нет активных аккаунтов.")
            return

        selected_client_data = random.choice(active_clients_data)
        selected_client_obj = None
        for cl_obj, cd_obj in userbot_clients_list:
            if cd_obj['id'] == selected_client_data['id']:
                selected_client_obj = cl_obj
                break

        if not selected_client_obj:
            logging.error(
                f"Не удалось найти объект клиента для аккаунта ID {selected_client_data['id']} для приветствия.")
            await bot.send_message(admin_chat_id,
                                   f"⚠️ Внутренняя ошибка при выборе аккаунта для приветствия в {entity_type} '{html.escape(target_chat_name)}'.")
            return

        welcome_prompt = get_config_value("ai_welcome_message_prompt", "Привет!")

        ai_message_text = await asyncio.to_thread(
            generate_vpn_comment,
            context=welcome_prompt,
            current_account_id_for_log=selected_client_data['id']
        )

        if not ai_message_text or "Извините" in ai_message_text or "не смог сформировать" in ai_message_text:
            logging.warning(f"AI не смог сгенерировать приветственное сообщение для {target_chat_name}.")
            add_analytics_log(account_id=selected_client_data['id'], action_type="welcome_message_ai_fail",
                              details=f"To chat: {target_chat_id}", success=False)
            await bot.send_message(admin_chat_id,
                                   f"⚠️ AI не смог сгенерировать приветствие для {entity_type} '{html.escape(target_chat_name)}'.")
            return

        try:
            await selected_client_obj.send_message(target_chat_id, ai_message_text, parse_mode='html',
                                                   link_preview=False)
            logging.info(
                f"Аккаунт {selected_client_data['label']} отправил приветственное сообщение в {entity_type} '{target_chat_name}' (ID: {target_chat_id}).")
            add_analytics_log(account_id=selected_client_data['id'], action_type="welcome_message_sent",
                              details=f"To chat: {target_chat_id} ({entity_type})", success=True)
            await send_report(
                report_title=f"Приветствие в {entity_type}",
                status="✅ Отправлено",
                account_info=f"<code>{selected_client_data['label']}</code> (ID: {selected_client_data['id']})",
                event_details=f"В {entity_type}: {html.escape(target_chat_name)} (<code>{target_chat_id}</code>)",
                response_info=f"<pre>{html.escape(ai_message_text[:300])}</pre>"
            )
        except (UserIsBlockedError, PeerIdInvalidError, ChatAdminRequiredError, ChannelPrivateError,
                FloodWaitError) as e_send:
            logging.error(
                f"Ошибка отправки приветственного сообщения в {target_chat_name} аккаунтом {selected_client_data['label']}: {e_send}")
            add_analytics_log(account_id=selected_client_data['id'], action_type="welcome_message_send_fail",
                              details=f"To chat: {target_chat_id}, Error: {type(e_send).__name__}",
                              success=False)
            await send_report(
                report_title=f"Приветствие в {entity_type}",
                status=f"❌ Ошибка отправки ({type(e_send).__name__})",
                account_info=f"<code>{selected_client_data['label']}</code> (ID: {selected_client_data['id']})",
                event_details=f"В {entity_type}: {html.escape(target_chat_name)} (<code>{target_chat_id}</code>)",
                response_info=f"<pre>{html.escape(ai_message_text[:300])}</pre>",
                error_info=str(e_send)
            )
        except Exception as e_unhandled:
            logging.error(f"Непредвиденная ошибка при отправке приветствия в {target_chat_name}: {e_unhandled}",
                          exc_info=True)
            add_analytics_log(account_id=selected_client_data['id'], action_type="welcome_message_unhandled_error",
                              details=f"To chat: {target_chat_id}, Error: {e_unhandled}", success=False)
            await send_report(
                report_title=f"Приветствие в {entity_type} (НЕОБРАБОТАННАЯ ОШИБКА)",
                status="❌ КРИТИЧЕСКАЯ ОШИБКА",
                account_info=f"<code>{selected_client_data['label']}</code> (ID: {selected_client_data['id']})",
                event_details=f"В {entity_type}: {html.escape(target_chat_name)} (<code>{target_chat_id}</code>)",
                error_info=str(e_unhandled)
            )

    except Exception as e_outer:
        logging.error(f"Общая ошибка в send_ai_welcome_message_to_chat для {target_chat_name}: {e_outer}",
                      exc_info=True)
        await bot.send_message(admin_chat_id,
                               f"💥 Произошла критическая ошибка при попытке отправить приветствие в {entity_type} '{html.escape(target_chat_name)}'. Подробности в логах.")


async def get_entity_info_robust(identifier: str, admin_chat_id: int, entity_type: str) -> dict:
    userbot_clients_list = get_active_clients()
    if not userbot_clients_list:
        raise Exception("Нет доступных аккаунтов для получения информации.")

    last_error = "Неизвестная ошибка"
    clients_to_check = list(userbot_clients_list)

    for client, client_data in clients_to_check:
        client_label = client_data.get('label', client.session.filename)
        try:
            entity = await client.get_entity(identifier)

            result = {
                "id": get_peer_id(entity),
                "username": getattr(entity, "username", None),
                "title": getattr(entity, "title", None),
                "client_obj": client,
                "client_data": client_data
            }

            return result

        except FATAL_ACCOUNT_ERRORS as e:
            logging.warning(
                f"Аккаунт '{client_label}' неактивен (Причина: {type(e).__name__}). Удаляю и пробую следующий.")
            await handle_banned_account(client_data, e, admin_chat_id)
            last_error = f"Аккаунт '{client_label}' был удален из-за бана/деактивации."
            continue

        except ValueError as e:
            if 'No user has' in str(e) or 'Cannot find any entity' in str(
                    e) or 'Could not find the input entity' in str(e):
                last_error = f"Сущность '{identifier}' не найдена аккаунтом '{client_label}'. Возможно, приватная или не существует."
                logging.warning(f"{last_error} ({type(e).__name__})")
                continue
            else:
                last_error = e
                logging.error(f"Неожиданный ValueError у '{client_label}' при поиске '{identifier}': {e}")
                continue

        except (InviteHashExpiredError, InviteHashInvalidError) as e:
            last_error = f"Ссылка-приглашение недействительна или истекла: {e}"
            break
        except (ChannelPrivateError, ChatAdminRequiredError):
            last_error = f"Канал/чат '{identifier}' приватный, и аккаунт '{client_label}' не имеет доступа."
            continue
        except Exception as e:
            last_error = e
            logging.warning(f"Ошибка у аккаунта '{client_label}' при поиске '{identifier}': {type(e).__name__} - {e}")
            continue

    raise Exception(
        f"Ни один из аккаунтов не смог получить информацию о '{identifier}'. Последняя ошибка: {last_error}")


async def join_group_with_client(client: TelegramClient, link_or_username: str, wait_on_flood: bool = True):
    link_input = link_or_username.strip()
    is_invite_link = False
    invite_hash_to_use = None

    original_link_check = link_or_username.strip()
    if original_link_check.startswith("https://t.me/+") or \
            original_link_check.startswith("http://t.me/+") or \
            original_link_check.startswith("t.me/+") or \
            original_link_check.startswith("+") or \
            "joinchat/" in original_link_check:
        is_invite_link = True
        if "joinchat/" in original_link_check:
            invite_hash_to_use = original_link_check.split("joinchat/")[-1].split("/")[0].strip()
        elif "+" in original_link_check:
            invite_hash_to_use = original_link_check.split("+")[-1].split("/")[0].strip()

        if not invite_hash_to_use and original_link_check.startswith("+"):
            invite_hash_to_use = original_link_check[1:]

    try:
        if is_invite_link and invite_hash_to_use:
            logging.info(
                f"Аккаунт {client.session.filename} пытается присоединиться по инвайт-хешу: {invite_hash_to_use}")
            await client(ImportChatInviteRequest(invite_hash_to_use))
        else:
            logging.info(
                f"Аккаунт {client.session.filename} пытается присоединиться к сущности: {link_or_username.strip()}")
            entity = await client.get_entity(link_or_username.strip())
            await client(JoinChannelRequest(entity))

        logging.info(f"Аккаунт {client.session.filename} успешно присоединился к {link_or_username}")

    except UserAlreadyParticipantError:
        logging.info(f"Аккаунт {client.session.filename} уже состоит в {link_or_username}.")
    except (InviteHashExpiredError, InviteHashInvalidError, ValueError, ChannelPrivateError) as e:
        logging.warning(
            f"Не удалось присоединить {client.session.filename} к {link_or_username}: {type(e).__name__} - {e}")
        raise e
    except FloodWaitError as e:
        logging.warning(
            f"FloodWait на {e.seconds} секунд для {client.session.filename} при попытке вступить в {link_or_username}. Ждём...")
        if not wait_on_flood:
            raise e
        await asyncio.sleep(e.seconds + 5)
        await join_group_with_client(client, link_or_username, wait_on_flood=wait_on_flood)
    except RPCError as e:
        logging.error(
            f"RPC ошибка при попытке вступления {client.session.filename} в {link_or_username}: {type(e).__name__} - {e}")
        raise e
    except Exception as e:
        logging.error(
            f"Неизвестная ошибка при попытке вступления {client.session.filename} в {link_or_username}: {type(e).__name__} - {e}")
        raise e


def _get_subscription_accounts_per_entity() -> int:
    return _get_int_config_value("subscription_accounts_per_entity", 5, 1)


def _get_account_subscription_loads() -> dict[int, int]:
    conn = get_db_connection()
    cursor = conn.cursor()
    loads: dict[int, int] = {}
    try:
        cursor.execute("""
            SELECT account_id, SUM(cnt) AS total_count
            FROM (
                SELECT assigned_account_id AS account_id, COUNT(*) AS cnt
                FROM channels
                WHERE assigned_account_id IS NOT NULL
                GROUP BY assigned_account_id
                UNION ALL
                SELECT assigned_account_id AS account_id, COUNT(*) AS cnt
                FROM groups
                WHERE assigned_account_id IS NOT NULL
                GROUP BY assigned_account_id
            )
            GROUP BY account_id
        """)
        loads = {int(row["account_id"]): int(row["total_count"] or 0) for row in cursor.fetchall()}
    except Exception as e:
        logging.warning(f"Could not load account subscription balance: {e}")
    finally:
        conn.close()
    return loads


def _order_clients_for_balanced_subscription(clients_pool: list[tuple[TelegramClient, dict]], runtime_loads: dict[int, int] | None = None):
    db_loads = _get_account_subscription_loads()
    runtime_loads = runtime_loads or {}
    return sorted(
        clients_pool,
        key=lambda item: (
            db_loads.get(item[1].get("id"), 0)
            + subscription_runtime_loads.get(_subscription_runtime_key(item[1].get("id")), 0)
            + runtime_loads.get(item[1].get("id"), 0),
            item[1].get("id") or 0,
        )
    )


async def leave_group_with_client(client: TelegramClient, chat_id_or_entity: int | str):
    userbot_clients_list = get_active_clients()
    try:
        await client(LeaveChannelRequest(chat_id_or_entity))
        logging.info(f"Аккаунт {client.session.filename} успешно покинул чат/канал {chat_id_or_entity}")
    except UserNotParticipantError:
        logging.info(f"Аккаунт {client.session.filename} не состоял в чате/канале {chat_id_or_entity}.")
    except (ValueError, ChannelPrivateError) as e:
        logging.warning(
            f"Не удалось покинуть {chat_id_or_entity} аккаунтом {client.session.filename}: {type(e).__name__} - {e}. Возможно, ID/ссылка неверна или чат недоступен.")
    except FloodWaitError as e:
        logging.warning(
            f"FloodWait на {e.seconds} секунд для {client.session.filename} при попытке покинуть {chat_id_or_entity}. Ждём...")
        await asyncio.sleep(e.seconds + 5)
        await leave_group_with_client(client, chat_id_or_entity)
    except RPCError as e:
        logging.error(
            f"RPC ошибка при попытке покинуть {chat_id_or_entity} аккаунтом {client.session.filename}: {type(e).__name__} - {e}")
    except Exception as e:
        logging.error(
            f"Неизвестная ошибка при попытке покинуть {chat_id_or_entity} аккаунтом {client.session.filename}: {type(e).__name__} - {e}")


async def remove_group_from_db_and_leave(group_id: int) -> str:
    userbot_clients_list = get_active_clients()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT title, username FROM groups WHERE id = ?", (group_id,))
    group_data = cursor.fetchone()
    group_display_name = group_data['title'] or group_data['username'] or str(group_id) if group_data else str(group_id)

    cursor.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    if not deleted:
        return f"Группа ({group_display_name}, ID={group_id}) не найдена в БД."

    errors_leaving = []
    if userbot_clients_list:
        for client, client_data_dict in userbot_clients_list:
            try:
                await leave_group_with_client(client, group_id)
                await asyncio.sleep(random.uniform(0.5, 1.5))
            except Exception as ex_leave:
                logging.info(
                    f"Ошибка при выходе из группы {group_id} аккаунтом {client_data_dict.get('session_name')}: {ex_leave}")
                errors_leaving.append(f"{client_data_dict.get('session_name')}: {str(ex_leave)[:100]}")
    else:
        logging.info("Нет активных клиентских аккаунтов для выполнения отписки.")

    if not errors_leaving:
        return f"Группа {group_display_name} (ID={group_id}) удалена из БД и все аккаунты успешно отписались."
    else:
        return f"Группа {group_display_name} (ID={group_id}) удалена из БД. При отписке некоторых аккаунтов возникли ошибки:\n" + "\n".join(
            errors_leaving)


async def mass_leave_entities_for_all_clients(admin_chat_id: int, entity_ids: list[int], entity_type: str,
                                              entity_names_for_log: list[str]):
    userbot_clients_list = get_active_clients()
    total_entities = len(entity_ids)
    if total_entities == 0:
        await bot.send_message(admin_chat_id, f"Нет {entity_type} для отписки.")
        return

    await bot.send_message(admin_chat_id,
                           f"🚀 Начинаю процесс отписки от {total_entities} {entity_type}. Это может занять некоторое время...")

    global_success_leaves = 0
    global_fail_leaves = 0

    processed_entity_count = 0

    for idx, entity_id in enumerate(entity_ids):
        entity_display_name = entity_names_for_log[idx] if idx < len(entity_names_for_log) else str(entity_id)
        processed_entity_count += 1

        current_entity_success_leaves = 0
        current_entity_fail_leaves = 0

        if processed_entity_count % 5 == 0 or processed_entity_count == 1 or processed_entity_count == total_entities:
            try:
                await bot.send_message(admin_chat_id,
                                       f"Отписываюсь от {entity_type} '{html.escape(entity_display_name)}' (ID: {entity_id}) [{processed_entity_count}/{total_entities}]...")
            except Exception:
                pass

        if not userbot_clients_list:
            logging.info(f"Нет активных клиентских аккаунтов для отписки от {entity_type} ID {entity_id}")
            continue

        for client, client_data in userbot_clients_list:
            client_label = client_data.get('label', client_data.get('session_name', 'N/A'))
            try:
                await leave_group_with_client(client, entity_id)
                current_entity_success_leaves += 1
                global_success_leaves += 1
                await asyncio.sleep(random.uniform(0.3, 1.0))
            except Exception as e_leave:
                current_entity_fail_leaves += 1
                global_fail_leaves += 1
                logging.warning(f"Аккаунт {client_label} не смог покинуть {entity_type} ID {entity_id}: {e_leave}")

        await asyncio.sleep(random.uniform(1, 3))

    summary_message = (
        f"🏁 Завершена массовая отписка от {total_entities} {entity_type}.\n\n"
        f"Всего успешных отписок аккаунтов от {entity_type}: {global_success_leaves}\n"
        f"Всего ошибок при отписке аккаунтов: {global_fail_leaves}"
    )
    await bot.send_message(admin_chat_id, summary_message)


async def subscribe_entity_logic(identifier: str, entity_type: str, admin_chat_id: int):
    stats = {'success': 0, 'failed': 0, 'deleted': 0}
    assigned_account_id = None
    subscription_mode = get_config_value("subscription_mode", "all_accounts")

    clients_to_try = list(get_active_clients())
    if not clients_to_try:
        logging.warning(f"Нет активных аккаунтов для подписки на '{identifier}'")
        return stats, assigned_account_id

    if subscription_mode == "single_account_sticky":
        max_retries = len(clients_to_try)
        for i in range(max_retries):
            client, client_data = get_next_account_in_cycle(f"assign_{entity_type}_cycle")
            if not client or not client_data:
                logging.warning("get_next_account_in_cycle не вернул доступный аккаунт, прерываю.")
                break

            label = client_data.get('label', 'N/A')
            logging.info(f"[STICKY MODE] Попытка #{i + 1} для '{identifier}' с аккаунтом '{label}'")

            try:
                await join_group_with_client(client, identifier)
                stats['success'] += 1
                assigned_account_id = client_data.get("id")
                logging.info(f"Аккаунт '{label}' успешно подписался на '{identifier}' и был назначен.")
                break

            except FATAL_ACCOUNT_ERRORS as e:
                logging.warning(
                    f"Аккаунт '{label}' неактивен (Причина: {type(e).__name__}). Удаляю и пробую следующий.")
                await handle_banned_account(client_data, e, admin_chat_id)
                stats['deleted'] += 1
                continue

            except (InviteHashExpiredError, InviteHashInvalidError, ValueError, ChannelPrivateError, RPCError) as e:
                stats['failed'] += 1
                logging.warning(f"Не удалось присоединить '{label}' к '{identifier}': {type(e).__name__} - {e}")
                break

            except UserAlreadyParticipantError:
                stats['success'] += 1
                assigned_account_id = client_data.get("id")
                logging.info(f"Аккаунт '{label}' уже в чате '{identifier}' и был назначен.")
                break

            except Exception as e:
                stats['failed'] += 1
                logging.error(f"Непредвиденная ошибка при присоединении '{label}' к '{identifier}': {e}", exc_info=True)
                break

    elif subscription_mode == "one_entity_five_accounts":
        target_successes = min(_get_subscription_accounts_per_entity(), len(clients_to_try))
        ordered_clients = _order_clients_for_balanced_subscription(clients_to_try)
        runtime_loads: dict[int, int] = {}
        for client, client_data in ordered_clients:
            if stats['success'] >= target_successes:
                break

            label = client_data.get('label', 'N/A')
            account_id = client_data.get("id")
            try:
                await join_group_with_client(client, identifier)
                stats['success'] += 1
                runtime_loads[account_id] = runtime_loads.get(account_id, 0) + 1
                runtime_key = _subscription_runtime_key(account_id)
                subscription_runtime_loads[runtime_key] = subscription_runtime_loads.get(runtime_key, 0) + 1
                if assigned_account_id is None:
                    assigned_account_id = account_id

            except FATAL_ACCOUNT_ERRORS as e:
                logging.warning(f"РђРєРєР°СѓРЅС‚ '{label}' РЅРµР°РєС‚РёРІРµРЅ (РџСЂРёС‡РёРЅР°: {type(e).__name__}). РЈРґР°Р»СЏСЋ.")
                await handle_banned_account(client_data, e, admin_chat_id)
                stats['deleted'] += 1

            except UserAlreadyParticipantError:
                stats['success'] += 1
                runtime_loads[account_id] = runtime_loads.get(account_id, 0) + 1
                runtime_key = _subscription_runtime_key(account_id)
                subscription_runtime_loads[runtime_key] = subscription_runtime_loads.get(runtime_key, 0) + 1
                if assigned_account_id is None:
                    assigned_account_id = account_id

            except Exception as e:
                stats['failed'] += 1
                logging.warning(f"РќРµ СѓРґР°Р»РѕСЃСЊ РїСЂРёСЃРѕРµРґРёРЅРёС‚СЊ '{label}' Рє '{identifier}': {type(e).__name__} - {e}")

            await asyncio.sleep(random.uniform(0.5, 1.5))

    else:
        for client, client_data in clients_to_try:
            label = client_data.get('label', 'N/A')
            try:
                await join_group_with_client(client, identifier)
                stats['success'] += 1

            except FATAL_ACCOUNT_ERRORS as e:
                logging.warning(f"Аккаунт '{label}' неактивен (Причина: {type(e).__name__}). Удаляю.")
                await handle_banned_account(client_data, e, admin_chat_id)
                stats['deleted'] += 1

            except UserAlreadyParticipantError:
                stats['success'] += 1

            except Exception as e:
                stats['failed'] += 1
                logging.warning(f"Не удалось присоединить '{label}' к '{identifier}': {type(e).__name__} - {e}")

            await asyncio.sleep(random.uniform(0.5, 1.5))

    return stats, assigned_account_id


async def subscribe_groups_bulk_in_bg(admin_chat_id: int, admin_user_id: int, links: list[str]):
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    workspace_id = get_active_workspace_id()
    description = f"Массовая подписка на {len(links)} групп"

    async def task_wrapper():
        try:
            total_links = len(links)
            total_stats = {'success': 0, 'failed': 0, 'deleted': 0, 'db_added': 0, 'db_failed': 0}
            processed_links_count = 0

            progress_title = f"Group subscription: {total_links} items"
            progress_message = await bot.send_message(
                admin_chat_id,
                _format_bulk_subscription_progress(
                    progress_title,
                    task_id,
                    total_links,
                    processed_links_count,
                    total_stats,
                    status="Starting group subscription."
                )
            )
            last_progress_update = 0.0

            async def update_progress(current_item: str = "", status: str = "", force: bool = False):
                nonlocal last_progress_update
                now = time.monotonic()
                if not force and (now - last_progress_update) < 2.5:
                    return
                last_progress_update = now
                try:
                    await bot.edit_message_text(
                        _format_bulk_subscription_progress(
                            progress_title,
                            task_id,
                            total_links,
                            processed_links_count,
                            total_stats,
                            current_item=current_item,
                            status=status
                        ),
                        chat_id=admin_chat_id,
                        message_id=progress_message.message_id
                    )
                except Exception as e_progress:
                    logging.debug(f"Bulk groups: progress edit skipped: {e_progress}")

            for idx, link_str in enumerate(links):
                processed_links_count += 1
                await update_progress(
                    current_item=link_str,
                    status=f"Processing item {processed_links_count}/{total_links}.",
                    force=(idx == 0 or idx % 3 == 0)
                )

                try:
                    stats, assigned_id = await subscribe_entity_logic(link_str, 'group', admin_chat_id)

                    total_stats['success'] += stats['success']
                    total_stats['failed'] += stats['failed']
                    total_stats['deleted'] += stats['deleted']

                    if stats['success'] > 0:
                        try:
                            group_info = await get_entity_info_robust(link_str, admin_chat_id, 'group')
                            conn_db = get_db_connection()
                            cursor_db = conn_db.cursor()
                            cursor_db.execute(
                                "INSERT OR REPLACE INTO groups (id, username, title, enabled, assigned_account_id) VALUES (?, ?, ?, 1, ?)",
                                (group_info["id"], group_info.get("username"), group_info.get("title"), assigned_id)
                            )
                            conn_db.commit()
                            conn_db.close()
                            total_stats['db_added'] += 1
                            asyncio.create_task(
                                send_ai_welcome_message_to_chat(
                                    admin_chat_id=admin_chat_id,
                                    target_chat_id=group_info["id"],
                                    target_chat_name=(
                                            group_info.get('title') or group_info.get('username') or link_str),
                                    entity_type="группу"
                                )
                            )
                            await update_progress(
                                current_item=link_str,
                                status="Joined and saved to database.",
                                force=True
                            )
                        except Exception as e_info_db:
                            total_stats['db_failed'] += 1
                            logging.error(f"Ошибка при получении инфо/добавлении в БД для '{link_str}': {e_info_db}")
                            await update_progress(
                                current_item=link_str,
                                status=f"DB/info error: {type(e_info_db).__name__}.",
                                force=True
                            )
                    else:
                        total_stats['db_failed'] += 1
                        logging.warning(f"Ни один аккаунт не смог подписаться на группу '{link_str}'.")
                        await update_progress(
                            current_item=link_str,
                            status="No account could join this group.",
                            force=True
                        )

                except asyncio.CancelledError:
                    logging.info(f"Task {task_id} for groups cancelled.")
                    await update_progress(
                        current_item=link_str,
                        status=f"Cancelled. Processed {idx + 1}/{total_links}.",
                        force=True
                    )
                    raise
                except Exception as e:
                    total_stats['db_failed'] += 1
                    logging.error(f"Bulk groups: critical error for {link_str}: {e}", exc_info=True)
                    await update_progress(
                        current_item=link_str,
                        status=f"Critical error: {type(e).__name__}.",
                        force=True
                    )

            summary_report = [
                f"<b>{description} finished.</b>",
                "",
                f"<code>{_progress_bar(total_links, total_links)}</code> 100%",
                f"Total groups: {total_links}",
                f"Processed: {processed_links_count}",
                f"Groups added to DB: {total_stats['db_added']}",
                f"DB/info failures: {total_stats['db_failed']}",
                f"Join successes: {total_stats['success']}",
                f"Join failures: {total_stats['failed']}",
                f"Deleted inactive accounts: {total_stats['deleted']}",
            ]
            try:
                await bot.edit_message_text(
                    "\n".join(summary_report),
                    chat_id=admin_chat_id,
                    message_id=progress_message.message_id
                )
            except Exception:
                await bot.send_message(admin_chat_id, "\n".join(summary_report))
        finally:
            if task_id in active_background_tasks:
                del active_background_tasks[task_id]
                logging.info(f"Task {task_id} ('{description}') finished and removed from registry.")

    task = asyncio.create_task(run_in_workspace(workspace_id, task_wrapper()))
    active_background_tasks[task_id] = {"task": task, "description": description, "workspace_id": workspace_id}
    await bot.send_message(
        admin_chat_id,
        f"Task '{description}' started with ID: `{task_id}`. Progress will update in one message."
    )


async def subscribe_channels_bulk_in_bg(admin_chat_id: int, admin_user_id: int, identifiers: list[str]):
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    workspace_id = get_active_workspace_id()
    description = f"Bulk channel subscription for {len(identifiers)} channels"
    batch_size = _get_int_config_value("channel_join_batch_size", 5, 1)
    cooldown_min = _get_int_config_value("channel_join_cooldown_min_seconds", 300, 1)
    cooldown_max = _get_int_config_value("channel_join_cooldown_max_seconds", 600, cooldown_min)
    if cooldown_max < cooldown_min:
        cooldown_max = cooldown_min
    attempt_gap_min = 8
    attempt_gap_max = 20

    async def task_wrapper():
        try:
            total_identifiers = len(identifiers)
            total_stats = {'success': 0, 'failed': 0, 'deleted': 0, 'db_added': 0, 'db_failed': 0}
            processed_count = 0
            subscription_mode = get_config_value("subscription_mode", "all_accounts")

            progress_title = f"Channel subscription: {total_identifiers} items"
            progress_message = await bot.send_message(
                admin_chat_id,
                _format_bulk_subscription_progress(
                    progress_title,
                    task_id,
                    total_identifiers,
                    processed_count,
                    total_stats,
                    status=f"Starting. Batch limit: {batch_size} joins/account, cooldown: {cooldown_min // 60}-{cooldown_max // 60} min."
                )
            )
            last_progress_update = 0.0

            async def update_progress(current_item: str = "", status: str = "", force: bool = False):
                nonlocal last_progress_update
                now = time.monotonic()
                if not force and (now - last_progress_update) < 2.5:
                    return
                last_progress_update = now
                try:
                    await bot.edit_message_text(
                        _format_bulk_subscription_progress(
                            progress_title,
                            task_id,
                            total_identifiers,
                            processed_count,
                            total_stats,
                            current_item=current_item,
                            status=status,
                            account_state=account_state if "account_state" in locals() else None
                        ),
                        chat_id=admin_chat_id,
                        message_id=progress_message.message_id
                    )
                except Exception as e_progress:
                    logging.debug(f"Bulk channels: progress edit skipped: {e_progress}")

            clients_pool = [(client, data) for client, data in get_active_clients() if client.is_connected()]
            if not clients_pool:
                await update_progress(status="No connected accounts for channel subscription.", force=True)
                return

            account_state = {}
            for client, data in clients_pool:
                account_id = data.get('id')
                account_state[account_id] = {
                    'attempts_in_batch': 0,
                    'available_at': 0.0,
                    'success': 0,
                    'failed': 0,
                    'deleted': 0,
                }

            async def wait_until_account_available():
                while True:
                    now = time.time()
                    available = [
                        (client, data) for client, data in clients_pool
                        if account_state[data.get('id')]['available_at'] <= now and client.is_connected()
                    ]
                    if available:
                        return available
                    next_available_at = min(account_state[data.get('id')]['available_at'] for _, data in clients_pool)
                    sleep_for = max(1, int(next_available_at - now))
                    await update_progress(
                        status=f"All accounts are resting. Waiting {sleep_for // 60}m {sleep_for % 60}s.",
                        force=True
                    )
                    await asyncio.sleep(sleep_for)

            async def wait_for_account(account_id: int):
                while True:
                    available_at = account_state[account_id]['available_at']
                    now = time.time()
                    if available_at <= now:
                        return
                    sleep_for = max(1, int(available_at - now))
                    await update_progress(
                        status=f"Account {account_id} is resting. Waiting {sleep_for // 60}m {sleep_for % 60}s.",
                        force=True
                    )
                    await asyncio.sleep(sleep_for)

            async def mark_attempt(account_id: int, reason: str = "batch"):
                state = account_state[account_id]
                state['attempts_in_batch'] += 1
                if state['attempts_in_batch'] >= batch_size:
                    cooldown = random.randint(cooldown_min, cooldown_max)
                    state['available_at'] = time.time() + cooldown
                    state['attempts_in_batch'] = 0
                    logging.info(f"Bulk channels: account {account_id} cooldown for {cooldown}s after {reason}.")
                    await update_progress(
                        status=f"Account {account_id}: rest {cooldown // 60}m {cooldown % 60}s after {batch_size} join attempts.",
                        force=True
                    )

            async def add_channel_to_db(identifier: str, assigned_id: int | None, info_client: TelegramClient):
                try:
                    try:
                        entity = await info_client.get_entity(identifier)
                        channel_info = {
                            "id": get_peer_id(entity),
                            "username": getattr(entity, "username", None),
                            "title": getattr(entity, "title", None),
                            "client_obj": info_client,
                        }
                    except Exception:
                        channel_info = await get_entity_info_robust(identifier, admin_chat_id, 'channel')

                    conn_db = get_db_connection()
                    cursor_db = conn_db.cursor()
                    cursor_db.execute(
                        "INSERT OR REPLACE INTO channels (id, username, title, enabled, linked_chat_id, assigned_account_id) VALUES (?, ?, ?, 1, ?, ?)",
                        (channel_info["id"], channel_info.get("username"), channel_info.get("title"),
                         channel_info.get("linked_chat_id"), assigned_id)
                    )
                    conn_db.commit()
                    conn_db.close()
                    total_stats['db_added'] += 1

                    channel_title_display = channel_info.get('title') or channel_info.get('username') or identifier
                    asyncio.create_task(
                        send_ai_welcome_message_to_chat(
                            admin_chat_id=admin_chat_id,
                            target_chat_id=channel_info["id"],
                            target_chat_name=channel_title_display,
                            entity_type="channel"
                        )
                    )
                    asyncio.create_task(
                        auto_handle_linked_chat_and_add_to_groups(
                            admin_chat_id,
                            channel_info,
                            channel_info.get('client_obj', info_client)
                        )
                    )
                except Exception as e_info_db:
                    total_stats['db_failed'] += 1
                    logging.error(f"Bulk channels: failed to get info/add DB for '{identifier}': {e_info_db}")
                    await update_progress(
                        current_item=identifier,
                        status=f"DB/info error: {type(e_info_db).__name__}.",
                        force=True
                    )

            async def try_join_with_account(client: TelegramClient, client_data: dict, identifier: str) -> tuple[bool, bool]:
                account_id = client_data.get('id')
                label = client_data.get('label', client_data.get('session_name', f"ID {account_id}"))
                try:
                    await join_group_with_client(client, identifier, wait_on_flood=False)
                    total_stats['success'] += 1
                    account_state[account_id]['success'] += 1
                    runtime_key = _subscription_runtime_key(account_id)
                    subscription_runtime_loads[runtime_key] = subscription_runtime_loads.get(runtime_key, 0) + 1
                    await mark_attempt(account_id, "successful join")
                    await asyncio.sleep(random.uniform(attempt_gap_min, attempt_gap_max))
                    return True, False
                except UserAlreadyParticipantError:
                    total_stats['success'] += 1
                    account_state[account_id]['success'] += 1
                    runtime_key = _subscription_runtime_key(account_id)
                    subscription_runtime_loads[runtime_key] = subscription_runtime_loads.get(runtime_key, 0) + 1
                    await mark_attempt(account_id, "already participant")
                    await asyncio.sleep(random.uniform(attempt_gap_min, attempt_gap_max))
                    return True, False
                except FloodWaitError as e:
                    total_stats['failed'] += 1
                    account_state[account_id]['failed'] += 1
                    cooldown = int(e.seconds) + random.randint(60, 180)
                    account_state[account_id]['available_at'] = time.time() + cooldown
                    account_state[account_id]['attempts_in_batch'] = 0
                    logging.warning(f"Bulk channels: FloodWait for account {label}: {e.seconds}s. Cooldown {cooldown}s.")
                    await update_progress(
                        current_item=identifier,
                        status=f"Account {label}: FloodWait {e.seconds}s. Rest {cooldown // 60}m {cooldown % 60}s.",
                        force=True
                    )
                    return False, True
                except FATAL_ACCOUNT_ERRORS as e:
                    total_stats['deleted'] += 1
                    account_state[account_id]['deleted'] += 1
                    await handle_banned_account(client_data, e, admin_chat_id)
                    logging.warning(f"Bulk channels: removed inactive account {label}: {type(e).__name__}")
                    return False, True
                except (InviteHashExpiredError, InviteHashInvalidError, ValueError, ChannelPrivateError, RPCError) as e:
                    total_stats['failed'] += 1
                    account_state[account_id]['failed'] += 1
                    await mark_attempt(account_id, f"failed join {type(e).__name__}")
                    logging.warning(f"Bulk channels: account {label} failed to join '{identifier}': {type(e).__name__} - {e}")
                    await asyncio.sleep(random.uniform(attempt_gap_min, attempt_gap_max))
                    return False, False
                except Exception as e:
                    total_stats['failed'] += 1
                    account_state[account_id]['failed'] += 1
                    await mark_attempt(account_id, "unexpected failure")
                    logging.warning(f"Bulk channels: account {label} unexpected join error for '{identifier}': {type(e).__name__} - {e}")
                    await asyncio.sleep(random.uniform(attempt_gap_min, attempt_gap_max))
                    return False, False

            sticky_index = 0
            for idx, ident_str in enumerate(identifiers):
                processed_count += 1
                await update_progress(
                    current_item=ident_str,
                    status=f"Processing item {processed_count}/{total_identifiers}.",
                    force=(idx == 0 or idx % 3 == 0)
                )

                try:
                    joined_by_any = False
                    assigned_id = None
                    info_client = None

                    if subscription_mode == "single_account_sticky":
                        attempts_left = len(clients_pool)
                        while attempts_left > 0 and not joined_by_any:
                            available_clients = await wait_until_account_available()
                            client, client_data = available_clients[sticky_index % len(available_clients)]
                            sticky_index += 1
                            attempts_left -= 1
                            ok, retry_later = await try_join_with_account(client, client_data, ident_str)
                            if ok:
                                joined_by_any = True
                                assigned_id = client_data.get('id')
                                info_client = client
                            elif retry_later:
                                continue
                    elif subscription_mode == "one_entity_five_accounts":
                        target_successes = min(_get_subscription_accounts_per_entity(), len(clients_pool))
                        success_count_for_entity = 0
                        runtime_loads = {
                            data.get('id'): account_state[data.get('id')]['success']
                            for _, data in clients_pool
                        }
                        ordered_clients = _order_clients_for_balanced_subscription(clients_pool, runtime_loads)
                        for client, client_data in ordered_clients:
                            if success_count_for_entity >= target_successes:
                                break
                            account_id = client_data.get('id')
                            await wait_for_account(account_id)
                            if not client.is_connected():
                                continue
                            ok, _ = await try_join_with_account(client, client_data, ident_str)
                            if ok:
                                success_count_for_entity += 1
                                joined_by_any = True
                                info_client = info_client or client
                                if assigned_id is None:
                                    assigned_id = account_id
                    else:
                        for client, client_data in list(clients_pool):
                            account_id = client_data.get('id')
                            await wait_for_account(account_id)
                            if not client.is_connected():
                                continue
                            ok, _ = await try_join_with_account(client, client_data, ident_str)
                            if ok:
                                joined_by_any = True
                                info_client = info_client or client

                    if joined_by_any:
                        await add_channel_to_db(ident_str, assigned_id, info_client)
                        await update_progress(
                            current_item=ident_str,
                            status="Joined and saved to database.",
                            force=True
                        )
                    else:
                        total_stats['db_failed'] += 1
                        logging.warning(f"Bulk channels: no account could join channel '{ident_str}'.")
                        await update_progress(
                            current_item=ident_str,
                            status="No account could join this channel.",
                            force=True
                        )

                except asyncio.CancelledError:
                    logging.info(f"Task {task_id} for channels cancelled.")
                    await update_progress(
                        current_item=ident_str,
                        status=f"Cancelled. Processed {idx + 1}/{total_identifiers}.",
                        force=True
                    )
                    raise
                except Exception as e:
                    total_stats['db_failed'] += 1
                    logging.error(f"Bulk channels: critical error for {ident_str}: {e}", exc_info=True)
                    await update_progress(
                        current_item=ident_str,
                        status=f"Critical error: {type(e).__name__}.",
                        force=True
                    )

            account_lines = []
            for _, data in clients_pool:
                account_id = data.get('id')
                label = data.get('label', data.get('session_name', str(account_id)))
                state = account_state[account_id]
                account_lines.append(
                    f"- {html.escape(str(label))} (ID {account_id}): ok {state['success']}, failed {state['failed']}, deleted {state['deleted']}"
                )

            summary_report = [
                f"<b>{description} finished.</b>",
                "",
                f"<code>{_progress_bar(total_identifiers, total_identifiers)}</code> 100%",
                f"Total channels: {total_identifiers}",
                f"Processed: {processed_count}",
                f"Channels added to DB: {total_stats['db_added']}",
                f"DB/info failures: {total_stats['db_failed']}",
                f"Join successes: {total_stats['success']}",
                f"Join failures: {total_stats['failed']}",
                f"Deleted inactive accounts: {total_stats['deleted']}",
                "",
                "<b>Per-account stats:</b>",
                *account_lines,
            ]
            try:
                await bot.edit_message_text(
                    "\n".join(summary_report),
                    chat_id=admin_chat_id,
                    message_id=progress_message.message_id
                )
            except Exception:
                await bot.send_message(admin_chat_id, "\n".join(summary_report))
        finally:
            if task_id in active_background_tasks:
                del active_background_tasks[task_id]
                logging.info(f"Task {task_id} ('{description}') finished and removed from registry.")

    task = asyncio.create_task(run_in_workspace(workspace_id, task_wrapper()))
    active_background_tasks[task_id] = {"task": task, "description": description, "workspace_id": workspace_id}
    await bot.send_message(
        admin_chat_id,
        f"Task '{description}' started with ID: `{task_id}`. Progress will update in one message."
    )


async def show_accounts_list(message_to_handle: Message, page: int, is_callback: bool = False):
    userbot_clients_list = get_active_clients()
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as cnt FROM accounts")
    row_count = cursor.fetchone()["cnt"]
    per_page = 10
    total_pages = (row_count + per_page - 1) // per_page or 1
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page

    cursor.execute("SELECT id, session_name, phone, label, user_id, is_enabled FROM accounts ORDER BY id LIMIT ? OFFSET ?",
                   (per_page, offset))
    accounts_from_db = cursor.fetchall()
    conn.close()

    header_text = f"Страница {page}/{total_pages}. Всего аккаунтов: {row_count}"
    if not accounts_from_db and row_count == 0:
        header_text = "Нет аккаунтов в базе."

    kb_buttons = []
    live_client_status_map = {}
    if userbot_clients_list:
        for cl_obj, cl_data in userbot_clients_list:
            s_name = cl_data.get("session_name")
            if s_name:
                is_conn = False
                try:
                    is_conn = cl_obj.is_connected()
                except:
                    pass
                live_client_status_map[s_name] = {"connected": is_conn, "uid": cl_data.get("user_id")}

    for acc_db_row in accounts_from_db:
        status_info = live_client_status_map.get(acc_db_row['session_name'])
        uid_display = acc_db_row['user_id'] or (status_info.get('uid') if status_info else None) or "N/A"
        is_enabled = acc_db_row['is_enabled'] if 'is_enabled' in acc_db_row.keys() else 1
        connection_icon = "🟢" if is_enabled and status_info and status_info['connected'] else "🔴"
        if not is_enabled:
            connection_icon = "⏸️"

        label_display = acc_db_row['label'] or acc_db_row['session_name'] or 'Без метки'
        max_label_len = 25
        button_text = f"{connection_icon} ID:{acc_db_row['id']} | {label_display[:max_label_len]}"
        if len(label_display) > max_label_len:
            button_text += "..."

        kb_buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"manage_account:{acc_db_row['id']}")])

    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"account_list_page:{page - 1}"))
    if total_pages > 1:
        nav_row.append(
            InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="ignore_page_btn_text_accounts"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"account_list_page:{page + 1}"))

    if nav_row:
        kb_buttons.append(nav_row)

    kb_buttons.append([InlineKeyboardButton(text="⬅️ Назад в настройки", callback_data="account_settings")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_buttons)

    try:
        if is_callback:
            await message_to_handle.edit_text(header_text, reply_markup=kb)
        else:
            await message_to_handle.answer(header_text, reply_markup=kb)
    except Exception as e:
        logging.warning(f"Failed to edit message for account list, sending new one: {e}")
        await bot.send_message(message_to_handle.chat.id, header_text, reply_markup=kb)


def generate_random_username_candidate(base_string: str, length: int = 5) -> str:
    name_part = ''.join(filter(str.isalnum, base_string)).lower()
    if not name_part: name_part = "user"

    max_base_len = 32 - length - 1
    name_part = name_part[:max_base_len]

    random_suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))
    candidate = f"{name_part}_{random_suffix}"

    while len(candidate) < 5:
        candidate += random.choice(string.digits)
    return candidate[:32]


async def execute_mass_profile_update(admin_chat_id: int, first_name: str, last_name: str = None,
                                      bio: str = None,
                                      photo_file_path: str = None,
                                      username_update_strategy: str = "skip"):
    userbot_clients_list = get_active_clients()
    await bot.send_message(admin_chat_id, "⏳ Начинаю массовое обновление профилей... Это может занять некоторое время.")
    success_count = 0
    fail_count = 0
    username_success_count = 0
    username_fail_count = 0

    active_clients_to_update = [client_tuple for client_tuple in userbot_clients_list if
                                client_tuple[0].is_connected()]
    total_clients = len(active_clients_to_update)

    if total_clients == 0:
        await bot.send_message(admin_chat_id, "Нет активных клиентских аккаунтов для обновления.",
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                                   text="⬅️ Назад в настройки аккаунтов", callback_data="account_settings")]]))
        if photo_file_path and os.path.exists(photo_file_path): os.remove(photo_file_path)
        return

    for i, (client, client_data) in enumerate(active_clients_to_update):
        s_name = client_data.get("label", client_data.get("session_name", f"аккаунт {i + 1}"))
        progress_text = f"🔄 Обновляю аккаунт {s_name} ({i + 1}/{total_clients})..."

        if i % 3 == 0 or i == 0 or i == total_clients - 1:
            try:
                await bot.send_message(admin_chat_id, progress_text)
            except Exception:
                pass

        current_first_name_for_username = first_name
        current_last_name_for_username = last_name if last_name is not None else ""

        try:
            params_to_update = {}
            if first_name:
                params_to_update['first_name'] = first_name
            if last_name is not None:
                params_to_update['last_name'] = last_name
            if bio is not None:
                params_to_update['about'] = bio

            if params_to_update: await client(UpdateProfileRequest(**params_to_update))

            if photo_file_path and os.path.exists(photo_file_path):
                uploaded_file = await client.upload_file(photo_file_path)
                await client(UploadProfilePhotoRequest(file=uploaded_file))

            success_count += 1

            if username_update_strategy == "random":
                username_set_successfully = False
                me_before_username_update = await client.get_me()

                base_for_username = f"{current_first_name_for_username or me_before_username_update.first_name or ''}{current_last_name_for_username or me_before_username_update.last_name or ''}"
                if not base_for_username.strip():
                    base_for_username = client_data.get("label", "telegramuser")

                for attempt in range(5):
                    candidate_username = generate_random_username_candidate(base_for_username, random.randint(3, 6))
                    try:
                        await client(UpdateUsernameRequest(candidate_username))
                        username_set_successfully = True
                        username_success_count += 1
                        logging.info(f"Successfully set username for {s_name} to {candidate_username}")
                        await bot.send_message(admin_chat_id,
                                               f"✅ Username для {s_name} установлен: @{candidate_username}")
                        break
                    except UsernameOccupiedError:
                        logging.info(
                            f"Username {candidate_username} is occupied for {s_name}. Attempt {attempt + 1}/5.")
                        await asyncio.sleep(0.5)
                    except UsernameInvalidError:
                        logging.warning(
                            f"Username {candidate_username} is invalid for {s_name}. Attempt {attempt + 1}/5.")
                        await asyncio.sleep(0.5)
                    except UsernameNotModifiedError:
                        username_set_successfully = True
                        username_success_count += 1
                        logging.info(f"Username for {s_name} is already {candidate_username}.")
                        break
                    except FloodWaitError as e_flood_user:
                        logging.error(
                            f"FloodWait setting username for {s_name}: {e_flood_user.seconds}s. Skipping username update for this account.")
                        await bot.send_message(admin_chat_id,
                                               f"🌊 FloodWait для username аккаунта {s_name}: ждем {e_flood_user.seconds} сек.")
                        await asyncio.sleep(e_flood_user.seconds + 1)
                        break
                if not username_set_successfully:
                    username_fail_count += 1
                    logging.warning(f"Failed to set a random username for {s_name} after multiple attempts.")
                    await bot.send_message(admin_chat_id, f"⚠️ Не удалось установить случайный username для {s_name}.")


        except FloodWaitError as e_flood:
            fail_count += 1
            logging.error(f"FloodWait при обновлении профиля {s_name}: {e_flood.seconds} сек.")
            await bot.send_message(admin_chat_id,
                                   f"⚠️ FloodWait для аккаунта {s_name}: ждем {e_flood.seconds} сек. Попробуйте позже.")
            await asyncio.sleep(e_flood.seconds + 2)
        except Exception as e:
            fail_count += 1
            logging.error(f"Не удалось обновить профиль для {s_name}: {e}")
            await bot.send_message(admin_chat_id, f"⚠️ Ошибка при обновлении аккаунта {s_name}: {e}")
        await asyncio.sleep(random.randint(1, 3))

    final_message_parts = [
        f"🏁 Массовое обновление профилей завершено!",
        f"👍 Успешно обновлено профилей (имя/био/фото): {success_count}",
        f"👎 Ошибок обновления профилей: {fail_count}"
    ]
    if username_update_strategy == "random":
        final_message_parts.append(f"👍 Успешно установлено username: {username_success_count}")
        final_message_parts.append(f"👎 Ошибок установки username: {username_fail_count}")

    final_message = "\n".join(final_message_parts)

    await bot.send_message(admin_chat_id, final_message, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад в настройки аккаунтов", callback_data="account_settings")]])
                           )

    if photo_file_path and os.path.exists(photo_file_path):
        try:
            os.remove(photo_file_path)
        except Exception as e:
            logging.error(f"Не удалось удалить временный файл фото {photo_file_path}: {e}")


async def auto_handle_linked_chat_and_add_to_groups(admin_chat_id: int, main_channel_info: dict,
                                                    client_to_use: TelegramClient) -> dict:
    if not client_to_use or not client_to_use.is_connected():
        return {"success": False, "message": "Предоставленный для поиска связанной группы клиент неактивен."}

    main_channel_id = main_channel_info.get("id")
    main_channel_title = main_channel_info.get('title') or main_channel_info.get('username') or str(main_channel_id)

    try:
        full_channel_details = await client_to_use(GetFullChannelRequest(channel=main_channel_id))
    except Exception as e:
        logging.error(
            f"Не удалось получить полную информацию для канала {main_channel_title} (ID: {main_channel_id}): {e}")
        return {"success": False,
                "message": f"Не удалось получить полную информацию для канала {html.escape(main_channel_title)}."}

    linked_chat_id_from_api = None
    if hasattr(full_channel_details.full_chat, 'linked_chat_id') and full_channel_details.full_chat.linked_chat_id:
        linked_chat_id_from_api = full_channel_details.full_chat.linked_chat_id

        original_linked_chat_id_in_db = main_channel_info.get("linked_chat_id")
        if str(original_linked_chat_id_in_db) != str(linked_chat_id_from_api):
            conn_update_lc = get_db_connection()
            cursor_update_lc = conn_update_lc.cursor()
            try:
                cursor_update_lc.execute("UPDATE channels SET linked_chat_id = ? WHERE id = ?",
                                         (str(linked_chat_id_from_api), main_channel_id))
                conn_update_lc.commit()
                logging.info(f"Обновлен linked_chat_id для канала {main_channel_id} на {linked_chat_id_from_api} в БД.")
            except Exception as e_db_lc:
                logging.error(f"Ошибка обновления linked_chat_id в БД для канала {main_channel_id}: {e_db_lc}")
            finally:
                conn_update_lc.close()

    if not linked_chat_id_from_api:
        logging.info(f"Канал {main_channel_title} не имеет связанной группы обсуждений.")
        return {"success": False,
                "message": f"Канал {html.escape(main_channel_title)} не имеет связанной группы обсуждений."}

    linked_chat_entity = None

    for chat_entity_in_list in full_channel_details.chats:
        if chat_entity_in_list.id == linked_chat_id_from_api:
            linked_chat_entity = chat_entity_in_list
            break

    if not linked_chat_entity:
        try:
            linked_chat_entity = await client_to_use.get_entity(linked_chat_id_from_api)
        except ValueError as ve:
            logging.warning(
                f"ValueError при get_entity для linked_chat_id {linked_chat_id_from_api}: {ve}. Пытаемся с префиксом -100, если ID положительный.")
            if isinstance(linked_chat_id_from_api, int) and linked_chat_id_from_api > 0:
                try:
                    corrected_linked_chat_id = int(f"-100{linked_chat_id_from_api}")
                    linked_chat_entity = await client_to_use.get_entity(corrected_linked_chat_id)
                    logging.info(
                        f"Успешно найдена сущность связанного чата с скорректированным ID: {corrected_linked_chat_id}")
                except Exception as e_corrected:
                    logging.error(
                        f"Не удалось получить сущность для linked_chat_id {linked_chat_id_from_api} (канал: {main_channel_title}) даже после коррекции: {e_corrected}")
                    return {"success": False,
                            "message": f"Не удалось получить информацию о связанной группе (ID: {linked_chat_id_from_api})."}
            else:
                logging.error(
                    f"Не удалось получить сущность для linked_chat_id {linked_chat_id_from_api} (канал: {main_channel_title}): {ve}")
                return {"success": False,
                        "message": f"Не удалось получить информацию о связанной группе (ID: {linked_chat_id_from_api})."}
        except Exception as e_get_entity_other:
            logging.error(
                f"Не удалось получить сущность для linked_chat_id {linked_chat_id_from_api} (канал: {main_channel_title}): {e_get_entity_other}")
            return {"success": False,
                    "message": f"Не удалось получить информацию о связанной группе (ID: {linked_chat_id_from_api})."}

    if not linked_chat_entity:
        return {"success": False,
                "message": f"Не удалось финализировать информацию о связанной группе (ID: {linked_chat_id_from_api})."}

    linked_chat_info_dict = {
        "id": linked_chat_entity.id,
        "username": getattr(linked_chat_entity, "username", None),
        "title": getattr(linked_chat_entity, "title", str(linked_chat_entity.id))
    }
    linked_chat_display_name = linked_chat_info_dict['title'] or linked_chat_info_dict['username'] or str(
        linked_chat_info_dict['id'])

    identifier_for_join_call = linked_chat_info_dict["username"] if linked_chat_info_dict["username"] else str(
        linked_chat_info_dict["id"])

    await bot.send_message(admin_chat_id,
                           f"Найдена связанная группа: <b>{html.escape(linked_chat_display_name)}</b> (ID: {linked_chat_info_dict['id']}). Подписываю аккаунты используя '{identifier_for_join_call}'...")

    stats, assigned_account_id = await subscribe_entity_logic(identifier_for_join_call, "group", admin_chat_id)

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT OR REPLACE INTO groups (id, username, title, enabled, assigned_account_id) VALUES (?, ?, ?, 1, ?)",
            (linked_chat_info_dict["id"], linked_chat_info_dict.get("username"), linked_chat_info_dict.get("title"),
             assigned_account_id)
        )
        conn.commit()
        logging.info(
            f"Связанная группа {linked_chat_display_name} (ID: {linked_chat_info_dict['id']}) добавлена/обновлена в таблице 'groups'.")
        await send_ai_welcome_message_to_chat(
            admin_chat_id=admin_chat_id,
            target_chat_id=linked_chat_info_dict["id"],
            target_chat_name=linked_chat_display_name,
            entity_type="группу обсуждения"
        )
    except Exception as e_db:
        logging.error(f"Ошибка добавления связанной группы {linked_chat_display_name} в БД: {e_db}")
        conn.rollback()
        await bot.send_message(admin_chat_id,
                               f"⚠️ Не удалось добавить связанную группу {html.escape(linked_chat_display_name)} в раздел 'Чаты': Ошибка БД.")
        return {"success": False, "message": "Ошибка при добавлении связанной группы в БД.",
                "linked_chat_info": linked_chat_info_dict}
    finally:
        conn.close()

    return {"success": True, "linked_chat_info": linked_chat_info_dict}
