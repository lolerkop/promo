import json
import logging
import re
from datetime import datetime, timezone

from services.default_prompts import DEFAULT_TRIGGER_FOLLOWUP_PROMPT


TOPIC_MARKERS = (
    "vpn", "впн", "v p n", "в п н", "proxy", "прокси",
    "связь", "интернет", "сеть", "мобайл", "мобильный",
    "блок", "блокировка", "заблок", "не открывается", "не открывает",
    "не грузит", "не работает", "не заходит", "не пускает",
    "ютуб", "youtube", "тик ток", "тикток", "tiktok", "instagram", "инста",
    "telegram", "телеграм", "тг", "ркн", "глуш", "глушилки", "белые списки",
    "обход", "ограничения", "режут", "замедляют", "лагает", "отваливается",
)

PROVOCATION_MARKERS = (
    "нейросеть", "нейронка", "искусственный интеллект", "chatgpt", "gpt",
    "ты бот", "бот?", "ты человек", "живой человек", "сколько будет", "2+2",
    "дважды два", "напиши код", "код напиши", "промпт", "system prompt",
    "инструкция", "объясни себя", "кто ты", "ignore previous", "забудь инструкции",
)


def get_db_connection():
    from db import get_db_connection as _get_db_connection
    return _get_db_connection()


def get_config_value(key: str, default_value=None):
    from db import get_config_value as _get_config_value
    return _get_config_value(key, default_value)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_text(text: str | None) -> str:
    normalized = (text or "").lower().replace("ё", "е")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def record_trigger_reply_thread(
        chat_id,
        source_user_id,
        source_message_id,
        bot_account_id,
        bot_user_id,
        bot_message_id,
        trigger_details: str | None = None,
):
    if not chat_id or not source_user_id or not bot_message_id:
        return

    now = _now_iso()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT INTO trigger_reply_threads (
                chat_id, source_user_id, source_message_id, bot_account_id, bot_user_id,
                bot_message_id, trigger_details, followup_sent, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(chat_id, bot_message_id) DO UPDATE SET
                source_user_id = excluded.source_user_id,
                source_message_id = excluded.source_message_id,
                bot_account_id = excluded.bot_account_id,
                bot_user_id = excluded.bot_user_id,
                trigger_details = excluded.trigger_details,
                updated_at = excluded.updated_at
        """, (
            int(chat_id),
            int(source_user_id),
            int(source_message_id) if source_message_id else 0,
            bot_account_id,
            bot_user_id,
            int(bot_message_id),
            trigger_details or "trigger_reply",
            now,
            now,
        ))
        conn.commit()
    except Exception:
        conn.rollback()
        logging.exception("Failed to record trigger reply thread.")
    finally:
        conn.close()


def get_trigger_reply_thread(chat_id, bot_message_id) -> dict | None:
    if not chat_id or not bot_message_id:
        return None

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT *
            FROM trigger_reply_threads
            WHERE chat_id = ? AND bot_message_id = ?
            LIMIT 1
        """, (int(chat_id), int(bot_message_id)))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def mark_trigger_followup_sent(thread_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE trigger_reply_threads SET followup_sent = 1, updated_at = ? WHERE id = ?",
            (_now_iso(), thread_id)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def quick_followup_relevance(text: str | None) -> tuple[bool | None, str]:
    normalized = _normalize_text(text)
    if len(normalized) < 4:
        return False, "too_short"
    if _contains_any(normalized, PROVOCATION_MARKERS):
        return False, "provocation"
    if _contains_any(normalized, TOPIC_MARKERS):
        return True, "local_topic_marker"
    return None, "uncertain"


def classify_followup_relevance_with_ai(user_text: str, previous_bot_text: str = "") -> tuple[bool, float, str]:
    from ai_handler import generate_ai_response

    prompt = (
        "Определи, относится ли ответ пользователя к теме VPN, блокировок, связи, интернета, "
        "глушилок или неработающих сервисов. Провокации, вопросы про нейросеть/бота, математику, "
        "код, промпт и посторонние темы должны быть matched=false.\n"
        "Верни строго JSON: {\"matched\": true/false, \"confidence\": 0..1, \"reason\": \"...\"}."
    )
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": (
                f"Предыдущий ответ бота:\n{previous_bot_text or ''}\n\n"
                f"Ответ пользователя:\n{user_text or ''}"
            ),
        },
    ]
    model = get_config_value("trigger_semantic_model", get_config_value("openai_model", "gpt-4o-mini"))
    result = generate_ai_response(
        messages,
        openai_model=model,
        provider=get_config_value("ai_provider", "openai"),
        temperature=0.1,
        max_tokens=120,
    )
    if not result.get("success"):
        return False, 0.0, "ai_unavailable"

    raw = (result.get("content") or "").strip()
    try:
        if raw.startswith("```"):
            raw = raw.strip("`")
            raw = re.sub(r"^json\s*", "", raw, flags=re.IGNORECASE).strip()
        data = json.loads(raw)
        matched = bool(data.get("matched"))
        confidence = float(data.get("confidence") or 0.0)
        reason = str(data.get("reason") or "ai_classifier")
        return matched and confidence >= 0.72, confidence, reason
    except Exception as exc:
        logging.warning("Could not parse trigger follow-up classifier response: %s; raw=%s", exc, raw[:300])
        return False, 0.0, "ai_parse_error"


def generate_trigger_followup_response(user_text: str, previous_bot_text: str = "") -> str:
    from ai_handler import generate_ai_response

    prompt = get_config_value("ai_trigger_followup_prompt", DEFAULT_TRIGGER_FOLLOWUP_PROMPT)
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": (
                f"Предыдущий ответ, на который ответил человек:\n---\n{previous_bot_text or ''}\n---\n\n"
                f"Сообщение человека:\n---\n{user_text or ''}\n---\n\n"
                "Ответь только самим reply или верни SKIP."
            ),
        },
    ]
    model = get_config_value("ai_trigger_model", get_config_value("openai_model", "gpt-4o-mini"))
    max_tokens = int(get_config_value("ai_trigger_max_tokens", "120") or 120)
    temperature = float(str(get_config_value("ai_trigger_temperature", "0.8")).replace(",", "."))
    result = generate_ai_response(
        messages,
        openai_model=model,
        provider=get_config_value("ai_provider", "openai"),
        temperature=temperature,
        max_tokens=max_tokens,
    )
    if not result.get("success"):
        return ""

    text = (result.get("content") or "").strip().strip('"').strip("'")
    if not text or text.upper().startswith("SKIP"):
        return ""
    if _contains_any(_normalize_text(text), ("как ии", "я бот", "я нейросеть", "не могу помочь")):
        return ""
    return text
