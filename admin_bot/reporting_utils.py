import logging
import html
import os
from datetime import datetime, timezone
from db import get_active_workspace_id, get_config_value  # ИСПРАВЛЕНО
from .bot_instance import bot


REPORTS_DIR = os.path.join("data", "reports")


def _ensure_reports_dir():
    os.makedirs(os.path.join(REPORTS_DIR, get_active_workspace_id()), exist_ok=True)


def get_auto_comment_report_file_path() -> str:
    _ensure_reports_dir()
    return os.path.join(REPORTS_DIR, get_active_workspace_id(), "auto_comment_reports.txt")


def clear_auto_comment_report_file():
    _ensure_reports_dir()
    with open(get_auto_comment_report_file_path(), "w", encoding="utf-8") as f:
        f.write("")


def _build_telegram_channel_link(channel_id, username: str = None, post_id: int = None) -> str:
    if username:
        clean_username = str(username).strip().lstrip("@")
        if clean_username:
            base_link = f"https://t.me/{clean_username}"
            return f"{base_link}/{post_id}" if post_id else base_link

    try:
        channel_id_int = int(channel_id)
        channel_id_str = str(abs(channel_id_int))
        internal_id = channel_id_str[3:] if channel_id_str.startswith("100") else channel_id_str
        base_link = f"https://t.me/c/{internal_id}"
        return f"{base_link}/{post_id}" if post_id else base_link
    except (TypeError, ValueError):
        return str(channel_id)


def append_auto_comment_report(
        channel_id,
        post_id: int,
        comment_text: str,
        account_id,
        account_label: str = None,
        channel_username: str = None,
        channel_title: str = None,
        sent_mode: str = None
):
    _ensure_reports_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    channel_link = _build_telegram_channel_link(channel_id, channel_username)
    post_link = _build_telegram_channel_link(channel_id, channel_username, post_id)
    account_label = account_label or "N/A"
    channel_title = channel_title or channel_username or str(channel_id)
    sent_mode = sent_mode or "N/A"
    comment_text = comment_text or ""

    entry = (
        f"===== {timestamp} =====\n"
        f"Channel: {channel_title}\n"
        f"Channel link: {channel_link}\n"
        f"Post link: {post_link}\n"
        f"Comment text:\n{comment_text}\n"
        f"Account: {account_label} (ID: {account_id if account_id is not None else 'N/A'})\n"
        f"Send mode: {sent_mode}\n\n"
    )

    with open(get_auto_comment_report_file_path(), "a", encoding="utf-8") as f:
        f.write(entry)


async def send_report(report_title: str, status: str, account_info: str, event_details: str, response_info: str = "",
                      error_info: str = ""):
    reporting_enabled = get_config_value("reporting_enabled", "False").lower() == 'true'
    if not reporting_enabled:
        return

    target_id_str = get_config_value("reporting_target_id")
    if not target_id_str:
        logging.warning("Reporting enabled but reporting_target_id is not set.")
        return

    try:
        target_id = int(target_id_str)
    except ValueError:
        logging.error(f"Invalid reporting_target_id: {target_id_str}")
        return

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    report_lines = [
        f"<b>🔔 Отчет: {html.escape(report_title)}</b>",
        f"<b>Статус:</b> {status}",
        f"<b>Аккаунт:</b> {account_info}",
        f"<b>Детали события:</b>\n{event_details}",
    ]
    if response_info:
        report_lines.append(f"<b>Ответ/Действие:</b>\n{response_info}")
    if error_info:
        report_lines.append(f"<b>Ошибка:</b>\n<pre>{html.escape(error_info)}</pre>")

    report_lines.append(f"\n<i>Время: {timestamp}</i>")

    full_report_html = "\n".join(report_lines)

    try:
        await bot.send_message(chat_id=target_id, text=full_report_html, parse_mode="HTML",
                               disable_web_page_preview=True)
    except Exception as e:
        logging.error(f"Failed to send report to {target_id}: {e}")
