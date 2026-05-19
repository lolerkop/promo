import asyncio
import logging
import time
import random
import html
from telethon import TelegramClient
from telethon.tl.types import Channel

# Импорты из вашего проекта
from db import (add_analytics_log, get_config_value, get_db_connection,
                record_account_runtime_event)
from userbot import get_next_account_in_cycle, generate_vpn_comment


async def _get_chat_title(client: TelegramClient, chat_id: int) -> str:
    """Безопасно получает заголовок чата."""
    try:
        entity = await client.get_entity(chat_id)
        return getattr(entity, 'title', f"ID: {chat_id}")
    except Exception as e:
        logging.warning(f"Не удалось получить заголовок для чата {chat_id}: {e}")
        return f"ID: {chat_id}"


async def send_regular_comment_now_core(initiated_by_admin: bool = True, last_usage_times_param: dict = None,
                                        target_chat_ids: list[int] = None, custom_prompt: str = None) -> dict:
    """Основная функция для отправки комментариев, возвращающая подробный отчет."""
    if last_usage_times_param is None:
        last_usage_times_param = {}

    report = {
        "success": False,
        "sent_to": [],
        "failed_for": [],
        "comment_text": "N/A",
        "account_label": "N/A",
        "account_id": None,
        "error": None
    }

    flood_gap = int(get_config_value("flood_gap_seconds", "60"))

    groups_to_process = []
    if target_chat_ids:
        groups_to_process = [{'id': chat_id} for chat_id in target_chat_ids]
    else:
        conn_main = get_db_connection()
        c_main = conn_main.cursor()
        c_main.execute("SELECT id FROM groups WHERE enabled=1")
        groups_from_db = c_main.fetchall()
        groups_to_process = [dict(row) for row in groups_from_db]
        conn_main.close()

    if not groups_to_process:
        report["error"] = "Нет чатов/групп для отправки."
        logging.info(f"[send_regular_comment_now_core] {report['error']}")
        return report

    client, acc_data = get_next_account_in_cycle("regular_comment_cycle")
    if not client or not acc_data:
        report["error"] = "Нет доступных аккаунтов для выполнения."
        logging.info(f"[send_regular_comment_now_core] {report['error']}")
        return report

    report["account_id"] = acc_data.get("id")
    report["account_label"] = acc_data.get("label", f"ID: {report['account_id']}")

    generated_text_base = generate_vpn_comment(
        context=custom_prompt if custom_prompt else "Комментарий по расписанию",
        current_account_id_for_log=report["account_id"]
    )
    report["comment_text"] = generated_text_base

    if not generated_text_base or "Извините" in generated_text_base or "не смог сформировать" in generated_text_base:
        report["error"] = "Пустой или ошибочный текст от AI."
        logging.info(f"[send_regular_comment_now_core] {report['error']}")
        return report

    account_key = acc_data.get("session_name", acc_data.get("phone"))

    for group_info in groups_to_process:
        chat_id = group_info['id']
        now_ts = time.time()
        time_since_last_use = now_ts - last_usage_times_param.get(account_key, 0)
        if time_since_last_use < flood_gap:
            sleep_duration = flood_gap - time_since_last_use
            await asyncio.sleep(sleep_duration)

        chat_title = await _get_chat_title(client, chat_id)

        try:
            await client.send_message(chat_id, generated_text_base, parse_mode='html', link_preview=False)
            last_usage_times_param[account_key] = time.time()
            record_account_runtime_event(report["account_id"], "regular_comment", True)
            logging.info(f"[send_regular_comment_now_core] Аккаунт {account_key} => Чат '{chat_title}' ({chat_id})")
            report["sent_to"].append({"chat_id": chat_id, "title": chat_title})
            await asyncio.sleep(random.randint(2, 5))
        except Exception as e:
            error_message = str(e)
            logging.error(
                f"[send_regular_comment_now_core] Ошибка в чате '{chat_title}' ({chat_id}) аккаунтом {account_key}: {error_message}")
            record_account_runtime_event(report["account_id"], "regular_comment", False, last_error=error_message[:500])
            report["failed_for"].append({"chat_id": chat_id, "title": chat_title, "error": error_message})
            if "FloodWait" in error_message:
                try:
                    wait_time = int(error_message.split("wait ")[1].split(" ")[0])
                    last_usage_times_param[account_key] = time.time() + wait_time
                except:
                    last_usage_times_param[account_key] = time.time() + flood_gap

    report["success"] = len(report["sent_to"]) > 0 and len(report["failed_for"]) == 0

    action_type_log = "category_comment_core" if target_chat_ids else "general_comment_core"
    add_analytics_log(account_id=report["account_id"], action_type=action_type_log,
                      details=f"Sent: {len(report['sent_to'])}, Errors: {len(report['failed_for'])}",
                      success=report["success"])

    return report
