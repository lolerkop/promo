# C:\Users\27030\Desktop\TEMP\main_app.py
import asyncio
import html
import logging
import random
import time
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from admin_bot.main import main_bot_polling_task
from app_core_utils import send_regular_comment_now_core
from db import (add_analytics_log, create_tables, get_config_value,
                get_db_connection, update_all_tables)
from shared import active_background_tasks
from userbot import (generate_vpn_comment, get_next_account_in_cycle,
                     start_all_clients)

try:
    from admin_bot.reporting_utils import send_report
except ImportError:
    async def send_report(*args, **kwargs):
        logging.error(
            "Failed to import send_report from admin_bot.reporting_utils in main_app.py. Reporting will be disabled.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
scheduler = AsyncIOScheduler(timezone="UTC")
last_usage_times = {}
category_last_run_times = {}
global_last_run_time = None
regular_scheduler_last_statuses = {}


def log_regular_scheduler_status_once(status_key: str, message: str):
    if regular_scheduler_last_statuses.get(status_key) == message:
        return
    regular_scheduler_last_statuses[status_key] = message
    logging.info(message)


def format_detailed_report(report_data: dict, title: str) -> dict:
    status = "Не определен"
    if report_data.get("error"):
        status = f"❌ Ошибка до отправки"
    elif report_data.get("success", False):
        status = "✅ Успешно"
    elif len(report_data.get("sent_to", [])) > 0:
        status = "⚠️ Частично успешно"
    else:
        status = "❌ Неудача"

    account_info = f"<code>{report_data.get('account_label', 'N/A')}</code> (ID: {report_data.get('account_id', 'N/A')})"

    details_parts = []
    if report_data.get("error"):
        details_parts.append(f"<b>Ошибка:</b> {html.escape(report_data['error'])}")

    if report_data.get("sent_to"):
        sent_list = "\n".join([f"- <i>{html.escape(item['title'])}</i> (<code>{item['chat_id']}</code>)" for item in
                               report_data["sent_to"]])
        details_parts.append(f"<b>✅ Отправлено в ({len(report_data['sent_to'])}):</b>\n{sent_list}")

    if report_data.get("failed_for"):
        failed_list = "\n".join([
            f"- <i>{html.escape(item['title'])}</i> (<code>{item['chat_id']}</code>)\n  Причина: {html.escape(item['error'])}"
            for item in report_data["failed_for"]])
        details_parts.append(f"<b>❌ Ошибки в ({len(report_data['failed_for'])}):</b>\n{failed_list}")

    event_details = "\n\n".join(details_parts)

    response_info = f"<b>Текст комментария:</b>\n<pre>{html.escape(report_data.get('comment_text', 'N/A'))}</pre>"

    return {
        "report_title": title,
        "status": status,
        "account_info": account_info,
        "event_details": event_details,
        "response_info": response_info,
        "error_info": ""
    }


async def hourly_task():
    client, acc_data = get_next_account_in_cycle("hourly_cycle")
    account_db_id = acc_data.get("id") if acc_data else None

    report_title = "Ежечасная рассылка в группы"
    report_status = "Не определен"
    report_account_info = f"<code>{acc_data.get('label', 'N/A') if acc_data else 'N/A'}</code> (ID: {account_db_id if account_db_id else 'N/A'})"
    report_event_details = "Подготовка к рассылке..."
    report_response_info = "Текст не сгенерирован"
    report_error_info = ""
    generated_text_for_report = "N/A"

    if not client:
        logging.info("[hourly_task] Нет аккаунтов для выполнения задачи.")
        add_analytics_log(account_id=None, action_type="hourly_task_skip", details="No accounts", success=False)
        report_status = "⚠️ Пропущено (нет аккаунтов)"
        report_event_details = "Нет доступных аккаунтов для выполнения задачи."
        await send_report(report_title, report_status, report_account_info, report_event_details, report_response_info,
                          report_error_info)
        return

    generated_text_for_report = generate_vpn_comment(context="Ежечасный запланированный пост",
                                                     current_account_id_for_log=account_db_id)
    report_response_info = f"Текст:\n<pre>{html.escape(generated_text_for_report[:400]) if generated_text_for_report else 'N/A'}</pre>"

    if not generated_text_for_report or "Извините" in generated_text_for_report or "не смог сформировать" in generated_text_for_report:
        logging.info(f"[hourly_task] Пустой или ошибочный текст от AI для аккаунта {account_db_id}.")
        add_analytics_log(account_id=account_db_id, action_type="hourly_task_ai_fail", details="Empty or error AI text",
                          success=False)
        report_status = "❌ Ошибка (AI не вернул текст)"
        report_event_details = "AI не смог сгенерировать текст для рассылки."
        await send_report(report_title, report_status, report_account_info, report_event_details, report_response_info,
                          report_error_info)
        return

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id FROM groups WHERE enabled=1")
    rows = c.fetchall()
    conn.close()

    report_event_details = f"Всего групп для рассылки: {len(rows)}"

    if not rows:
        logging.info("[hourly_task] Нет групп для отправки.")
        add_analytics_log(account_id=account_db_id, action_type="hourly_task_skip", details="No groups", success=False)
        report_status = "⚠️ Пропущено (нет групп для отправки)"
        await send_report(report_title, report_status, report_account_info, report_event_details, report_response_info,
                          report_error_info)
        return

    sent_count = 0
    error_count = 0
    group_errors = []

    for r in rows:
        try:
            await client.send_message(
                entity=r["id"],
                message=generated_text_for_report,
                parse_mode='html',
                link_preview=False
            )
            logging.info(
                f"[hourly_task] Аккаунт {acc_data.get('label', 'N/A')} отправил комментарий в группу {r['id']}.")
            sent_count += 1
            await asyncio.sleep(random.randint(2, 5))
        except Exception as e:
            logging.error(
                f"[hourly_task] Ошибка при отправке в группу {r['id']} аккаунтом {acc_data.get('label', 'N/A')}: {e}")
            error_count += 1
            group_errors.append(f"Группа {r['id']}: {str(e)[:100]}")

    add_analytics_log(account_id=account_db_id, action_type="hourly_task_execution",
                      details=f"Sent: {sent_count}, Errors: {error_count}",
                      success=(error_count == 0 and sent_count > 0))

    if error_count == 0 and sent_count > 0:
        report_status = f"✅ Успешно (отправлено: {sent_count})"
    elif sent_count > 0:
        report_status = f"⚠️ Частично (отправлено: {sent_count}, ошибок: {error_count})"
    else:
        report_status = f"❌ Ошибка (отправлено: {sent_count}, ошибок: {error_count})"

    if group_errors:
        report_error_info = "\n".join(group_errors)

    await send_report(report_title, report_status, report_account_info, report_event_details, report_response_info,
                      report_error_info)


async def regular_comment_task_for_scheduler():
    global last_usage_times, category_last_run_times, global_last_run_time
    now = datetime.now()

    if get_config_value("REGULAR_COMMENT_ENABLED", "0") == "1":
        try:
            interval_minutes = int(get_config_value("REGULAR_COMMENT_INTERVAL", "60"))
        except ValueError:
            interval_minutes = 60
            log_regular_scheduler_status_once(
                "regular_global_config",
                "[regular_comment_scheduler] Invalid REGULAR_COMMENT_INTERVAL, using 60 minutes."
            )

        if global_last_run_time is None or (now - global_last_run_time) >= timedelta(minutes=interval_minutes):
            regular_scheduler_last_statuses.pop("regular_global_wait", None)
            log_regular_scheduler_status_once(
                "regular_global",
                f"[regular_comment_scheduler] Global regular comment is due. Interval: {interval_minutes} min."
            )
            logging.info(
                f"[Scheduler] Запуск задачи регулярного комментария для ОБЩИХ ГРУПП (интервал: {interval_minutes} мин)...")
            report_result = await send_regular_comment_now_core(
                initiated_by_admin=False,
                last_usage_times_param=last_usage_times
            )
            global_last_run_time = now
            if report_result.get("sent_to") or report_result.get("failed_for") or report_result.get("error"):
                formatted_report = format_detailed_report(report_result, "Регулярный автокомментарий (Общие группы)")
                await send_report(**formatted_report)
        else:
            next_run_at = global_last_run_time + timedelta(minutes=interval_minutes)
            log_regular_scheduler_status_once(
                "regular_global_wait",
                f"[regular_comment_scheduler] Global regular comment is enabled, waiting for interval. Next run after {next_run_at:%Y-%m-%d %H:%M:%S}."
            )
    else:
        log_regular_scheduler_status_once(
            "regular_global",
            "[regular_comment_scheduler] Global regular comment is disabled."
        )

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM chat_categories WHERE regular_comment_enabled = 1")
    categories = [dict(row) for row in c.fetchall()]
    conn.close()

    if not categories:
        log_regular_scheduler_status_once(
            "regular_categories",
            "[regular_comment_scheduler] No enabled category regular-comment jobs."
        )
    else:
        log_regular_scheduler_status_once(
            "regular_categories",
            f"[regular_comment_scheduler] Enabled category regular-comment jobs: {len(categories)}."
        )

    for category in categories:
        category_name = category["category_name"]
        interval = category["regular_comment_interval_minutes"]
        last_run = category_last_run_times.get(category_name)

        if last_run and (now - last_run) < timedelta(minutes=interval):
            next_category_run_at = last_run + timedelta(minutes=interval)
            log_regular_scheduler_status_once(
                f"regular_category_wait:{category_name}",
                f"[regular_comment_scheduler] Category '{category_name}' is waiting for interval. Next run after {next_category_run_at:%Y-%m-%d %H:%M:%S}."
            )
            continue

        logging.info(f"[Scheduler] Запуск задачи регулярного комментария для КАТЕГОРИИ: {category_name}")
        regular_scheduler_last_statuses.pop(f"regular_category_wait:{category_name}", None)
        log_regular_scheduler_status_once(
            f"regular_category_run:{category_name}",
            f"[regular_comment_scheduler] Category '{category_name}' regular comment is due. Interval: {interval} min."
        )
        category_last_run_times[category_name] = now

        conn_cats = get_db_connection()
        c_cats = conn_cats.cursor()
        c_cats.execute("SELECT chat_id FROM category_joined_chats WHERE category_name = ?", (category_name,))
        chat_ids = [row['chat_id'] for row in c_cats.fetchall()]
        conn_cats.close()

        if not chat_ids:
            logging.info(f"В категории '{category_name}' нет чатов для комментирования.")
            await send_report(
                report_title=f"Автокомментарий (Категория: {category_name})",
                status="⚠️ Пропущено",
                account_info="N/A",
                event_details=f"Для категории '{category_name}' нет чатов для обработки.",
            )
            continue

        report_result_cat = await send_regular_comment_now_core(
            initiated_by_admin=False,
            last_usage_times_param=last_usage_times,
            target_chat_ids=chat_ids,
            custom_prompt=category["regular_comment_prompt"]
        )

        formatted_report_cat = format_detailed_report(
            report_result_cat,
            f"Автокомментарий (Категория: {category_name})"
        )
        await send_report(**formatted_report_cat)


async def main_app_send_regular_comment_now(
        initiated_by_admin: bool = True):
    global last_usage_times
    return await send_regular_comment_now_core(initiated_by_admin=initiated_by_admin,
                                               last_usage_times_param=last_usage_times)


def main_app_schedule_regular_comment_job():
    global scheduler
    scheduler.add_job(regular_comment_task_for_scheduler, 'interval', minutes=1, id='regular_comment_central_scheduler')


async def main():
    create_tables()
    update_all_tables()

    global last_usage_times, category_last_run_times, global_last_run_time, regular_scheduler_last_statuses
    last_usage_times = {}
    category_last_run_times = {}
    global_last_run_time = None
    regular_scheduler_last_statuses = {}

    logging.info("Запуск клиентских аккаунтов Telethon...")
    telethon_client_tasks = await start_all_clients()
    if not telethon_client_tasks:
        logging.warning("Ни один клиент Telethon не был запущен.")

    logging.info("Запуск админ-бота Aiogram...")
    admin_bot_task = asyncio.create_task(main_bot_polling_task())

    try:
        scheduler.start()
        main_app_schedule_regular_comment_job()
        logging.info("Планировщик задач запущен.")
    except Exception as e:
        logging.error(f"Не удалось запустить планировщик: {e}")

    all_tasks = telethon_client_tasks + [admin_bot_task]

    try:
        await asyncio.gather(*all_tasks)
    except asyncio.CancelledError:
        logging.info("Основная задача была отменена. Начинается завершение работы...")
    finally:
        logging.info("Остановка всех задач...")
        for task in all_tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*all_tasks, return_exceptions=True)

        if scheduler.running:
            scheduler.shutdown()
            logging.info("Планировщик остановлен.")

        logging.info("Приложение завершило работу.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Приложение остановлено вручную или системой.")
