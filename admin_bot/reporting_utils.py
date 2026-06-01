import logging
import html
import os
from datetime import datetime, timezone
from db import get_config_value, get_current_workspace_id  # ИСПРАВЛЕНО
from .bot_instance import bot


REPORTS_DIR = os.path.join("data", "reports")


def _ensure_reports_dir():
    os.makedirs(os.path.join(REPORTS_DIR, get_current_workspace_id()), exist_ok=True)


def get_auto_comment_report_file_path() -> str:
    _ensure_reports_dir()
    return os.path.join(REPORTS_DIR, get_current_workspace_id(), "auto_comment_reports.txt")


def get_trigger_reply_report_file_path() -> str:
    _ensure_reports_dir()
    return os.path.join(REPORTS_DIR, get_current_workspace_id(), "trigger_reply_reports.txt")


def clear_auto_comment_report_file():
    _ensure_reports_dir()
    with open(get_auto_comment_report_file_path(), "w", encoding="utf-8") as f:
        f.write("")


def clear_trigger_reply_report_file():
    _ensure_reports_dir()
    with open(get_trigger_reply_report_file_path(), "w", encoding="utf-8") as f:
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


def append_trigger_reply_report(
        chat_id,
        source_message_id: int,
        reply_message_id: int,
        response_text: str,
        account_id,
        account_label: str = None,
        trigger_details: str = None,
        original_text: str = None,
        chat_username: str = None,
        chat_title: str = None
):
    _ensure_reports_dir()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    chat_title = chat_title or chat_username or str(chat_id)
    account_label = account_label or "N/A"
    trigger_details = trigger_details or "trigger_reply"
    original_text = original_text or ""
    response_text = response_text or ""
    chat_link = _build_telegram_channel_link(chat_id, chat_username)
    source_link = _build_telegram_channel_link(chat_id, chat_username, source_message_id)
    reply_link = _build_telegram_channel_link(chat_id, chat_username, reply_message_id)

    entry = (
        f"===== {timestamp} =====\n"
        f"Chat: {chat_title}\n"
        f"Chat ID: {chat_id}\n"
        f"Chat link: {chat_link}\n"
        f"Original message link: {source_link}\n"
        f"Bot reply link: {reply_link}\n"
        f"Trigger: {trigger_details}\n"
        f"Original text:\n{original_text}\n"
        f"Reply text:\n{response_text}\n"
        f"Account: {account_label} (ID: {account_id if account_id is not None else 'N/A'})\n\n"
    )

    with open(get_trigger_reply_report_file_path(), "a", encoding="utf-8") as f:
        f.write(entry)


def _split_report_entries(text: str) -> list[str]:
    entries = []
    current = []
    for line in text.splitlines():
        if line.startswith("===== ") and current:
            entries.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        entry = "\n".join(current).strip()
        if entry:
            entries.append(entry)
    return entries


def _extract_report_field(entry: str, field_name: str) -> str:
    prefix = f"{field_name}: "
    for line in entry.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def get_recent_trigger_reply_reports(limit: int = 20) -> list[dict]:
    path = get_trigger_reply_report_file_path()
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []

    with open(path, "r", encoding="utf-8") as f:
        entries = _split_report_entries(f.read())

    recent = []
    for entry in entries[-limit:]:
        recent.append({
            "timestamp": entry.splitlines()[0].strip("= ").strip() if entry.splitlines() else "",
            "chat": _extract_report_field(entry, "Chat"),
            "chat_id": _extract_report_field(entry, "Chat ID"),
            "reply_link": _extract_report_field(entry, "Bot reply link"),
            "trigger": _extract_report_field(entry, "Trigger"),
            "account": _extract_report_field(entry, "Account"),
            "raw": entry,
        })
    return recent


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
