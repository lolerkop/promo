import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ai_handler import generate_ai_response
from db import get_config_value, get_db_connection, set_config_value


TRUE_VALUES = {"1", "true", "yes", "on", "да", "вкл"}
VPN_ALIASES = {
	"vpn": {"vpn", "впн"},
	"впн": {"vpn", "впн"},
}

_semantic_rate_window: list[float] = []
_trigger_delivery_state: dict[tuple, float] = {}


@dataclass
class TriggerDecision:
	matched: bool = False
	source: str = ""
	match_type: str = ""
	keyword: str | None = None
	intent_id: int | None = None
	intent_name: str | None = None
	response_type: str | None = None
	answer: str = ""
	confidence: float = 0.0
	reason: str = ""
	category_name: str | None = None
	semantic: bool = False

	@property
	def action_details(self) -> str:
		if self.intent_name:
			return f"trigger_intent:{self.intent_name}"
		if self.category_name and self.keyword:
			return f"category_keyword_reply:{self.category_name}:{self.keyword}"
		if self.keyword:
			return f"keyword_reply:{self.keyword}"
		return "trigger_reply"

	@property
	def cooldown_key(self) -> str:
		return self.intent_name or self.keyword or self.source or "trigger"


def _as_bool(value: str | None, default: bool = False) -> bool:
	if value is None:
		return default
	return str(value).strip().lower() in TRUE_VALUES


def _as_float(value: str | None, default: float) -> float:
	try:
		return float(str(value).replace(",", "."))
	except (TypeError, ValueError):
		return default


def _as_int(value: str | None, default: int) -> int:
	try:
		return int(str(value).strip())
	except (TypeError, ValueError):
		return default


def normalize_text(text: str | None) -> str:
	text = unicodedata.normalize("NFKC", text or "")
	text = text.casefold().replace("ё", "е")
	cleaned_chars = []
	for char in text:
		cleaned_chars.append(char if char.isalnum() else " ")
	return re.sub(r"\s+", " ", "".join(cleaned_chars)).strip()


def compact_text(text: str | None) -> str:
	return normalize_text(text).replace(" ", "")


def _phrase_matches_text(phrase: str, normalized_text: str, compacted_text: str) -> bool:
	normalized_phrase = normalize_text(phrase)
	if not normalized_phrase:
		return False
	if normalized_phrase in normalized_text:
		return True

	phrase_tokens = normalized_phrase.split()
	text_tokens = normalized_text.split()
	if len(phrase_tokens) > 1 and len(text_tokens) >= len(phrase_tokens):
		position = 0
		for token in text_tokens:
			if token == phrase_tokens[position]:
				position += 1
				if position == len(phrase_tokens):
					return True

	compacted_phrase = normalized_phrase.replace(" ", "")
	if len(compacted_phrase) < 3:
		return False
	alias_values = VPN_ALIASES.get(compacted_phrase, {compacted_phrase})
	return any(alias in compacted_text for alias in alias_values)


def _specificity_score(phrase: str, source_priority: int) -> tuple[int, int, int]:
	normalized_phrase = normalize_text(phrase)
	return (source_priority, len(normalized_phrase.split()), len(normalized_phrase))


def _row_value(row: Any, key: str, default=None):
	try:
		value = row[key]
	except (KeyError, IndexError, TypeError):
		value = None
	if value is None and isinstance(row, dict):
		value = row.get(key)
	return default if value is None else value


def _candidate_decision(
	row: Any,
	source: str,
	source_priority: int,
	keyword_key: str = "keyword",
	category_name: str | None = None,
) -> tuple[tuple[int, int, int], TriggerDecision]:
	keyword = str(_row_value(row, keyword_key, "") or "").strip()
	response_type = str(_row_value(row, "response_type", "predefined") or "predefined")
	answer = str(_row_value(row, "answer", "") or "")
	intent_id = _row_value(row, "intent_id")
	intent_name = _row_value(row, "intent_name")
	match_type = str(_row_value(row, "match_type", "keyword") or "keyword")
	decision = TriggerDecision(
		matched=True,
		source=source,
		match_type=match_type,
		keyword=keyword,
		intent_id=int(intent_id) if intent_id else None,
		intent_name=str(intent_name) if intent_name else None,
		response_type=response_type,
		answer=answer,
		confidence=1.0,
		reason="local_match",
		category_name=category_name,
		semantic=False,
	)
	return _specificity_score(keyword, source_priority), decision


def best_local_trigger_match(
	message_text: str | None,
	category_keywords: list[Any] | None = None,
	global_responses: list[Any] | None = None,
	intent_phrase_rows: list[Any] | None = None,
	category_name: str | None = None,
) -> TriggerDecision:
	normalized = normalize_text(message_text)
	compacted = normalized.replace(" ", "")
	if not normalized:
		return TriggerDecision(reason="empty_message")

	candidates: list[tuple[tuple[int, int, int], TriggerDecision]] = []

	for row in category_keywords or []:
		keyword = str(_row_value(row, "keyword", "") or "")
		if _phrase_matches_text(keyword, normalized, compacted):
			candidates.append(_candidate_decision(row, "category", 300, category_name=category_name))

	for row in intent_phrase_rows or []:
		phrase = str(_row_value(row, "phrase", "") or "")
		match_type = str(_row_value(row, "match_type", "phrase") or "phrase")
		if match_type == "semantic":
			continue
		if _phrase_matches_text(phrase, normalized, compacted):
			candidates.append(_candidate_decision(row, "intent_phrase", 200, keyword_key="phrase"))

	for row in global_responses or []:
		keyword = str(_row_value(row, "keyword", "") or "")
		if _phrase_matches_text(keyword, normalized, compacted):
			candidates.append(_candidate_decision(row, "global", 100))

	if not candidates:
		return TriggerDecision(reason="no_local_match")

	candidates.sort(key=lambda item: item[0], reverse=True)
	return candidates[0][1]


def fetch_category_keywords(category_name: str | None) -> list[dict]:
	if not category_name:
		return []
	conn = get_db_connection()
	cursor = conn.cursor()
	try:
		cursor.execute(
			"SELECT keyword, answer, response_type FROM category_keywords WHERE category_name = ?",
			(category_name,),
		)
		return [dict(row) for row in cursor.fetchall()]
	finally:
		conn.close()


def fetch_global_responses() -> list[dict]:
	conn = get_db_connection()
	cursor = conn.cursor()
	try:
		cursor.execute("SELECT keyword, answer, response_type FROM responses")
		return [dict(row) for row in cursor.fetchall()]
	finally:
		conn.close()


def fetch_intent_phrase_rows() -> list[dict]:
	conn = get_db_connection()
	cursor = conn.cursor()
	try:
		cursor.execute("""
			SELECT
				tip.phrase,
				tip.match_type,
				ti.id AS intent_id,
				ti.name AS intent_name,
				ti.response_type,
				ti.answer
			FROM trigger_intent_phrases tip
			JOIN trigger_intents ti ON ti.id = tip.intent_id
			WHERE ti.is_active = 1 AND tip.is_active = 1
		""")
		return [dict(row) for row in cursor.fetchall()]
	finally:
		conn.close()


def fetch_trigger_intents(include_phrases: bool = True, active_only: bool = False) -> list[dict]:
	conn = get_db_connection()
	cursor = conn.cursor()
	try:
		where = "WHERE is_active = 1" if active_only else ""
		cursor.execute(f"""
			SELECT id, name, description, response_type, answer, is_active, semantic_enabled
			FROM trigger_intents
			{where}
			ORDER BY name
		""")
		intents = [dict(row) for row in cursor.fetchall()]
		if include_phrases and intents:
			intent_ids = [intent["id"] for intent in intents]
			placeholders = ",".join("?" for _ in intent_ids)
			cursor.execute(f"""
				SELECT id, intent_id, phrase, match_type, is_active
				FROM trigger_intent_phrases
				WHERE intent_id IN ({placeholders})
				ORDER BY phrase
			""", intent_ids)
			phrases_by_intent: dict[int, list[dict]] = {}
			for row in cursor.fetchall():
				phrases_by_intent.setdefault(row["intent_id"], []).append(dict(row))
			for intent in intents:
				intent["phrases"] = phrases_by_intent.get(intent["id"], [])
		return intents
	finally:
		conn.close()


def get_trigger_intent(intent_id: int, include_phrases: bool = True) -> dict | None:
	conn = get_db_connection()
	cursor = conn.cursor()
	try:
		cursor.execute("""
			SELECT id, name, description, response_type, answer, is_active, semantic_enabled
			FROM trigger_intents
			WHERE id = ?
		""", (intent_id,))
		row = cursor.fetchone()
		if not row:
			return None
		intent = dict(row)
		if include_phrases:
			cursor.execute("""
				SELECT id, intent_id, phrase, match_type, is_active
				FROM trigger_intent_phrases
				WHERE intent_id = ?
				ORDER BY phrase
			""", (intent_id,))
			intent["phrases"] = [dict(phrase_row) for phrase_row in cursor.fetchall()]
		return intent
	finally:
		conn.close()


def update_trigger_intent(intent_id: int, **fields) -> dict | None:
	allowed = {"name", "description", "response_type", "answer", "is_active", "semantic_enabled"}
	updates = {key: value for key, value in fields.items() if key in allowed}
	if not updates:
		return get_trigger_intent(intent_id)
	updates["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
	assignments = ", ".join(f"{key} = ?" for key in updates)
	values = list(updates.values()) + [intent_id]
	conn = get_db_connection()
	cursor = conn.cursor()
	try:
		cursor.execute(f"UPDATE trigger_intents SET {assignments} WHERE id = ?", values)
		conn.commit()
	finally:
		conn.close()
	return get_trigger_intent(intent_id)


def upsert_trigger_intent(
	name: str,
	description: str = "",
	response_type: str = "openai",
	answer: str = "",
	is_active: int = 1,
	semantic_enabled: int = 1,
) -> int:
	name = (name or "").strip()
	if not name:
		raise ValueError("Intent name cannot be empty")
	now = datetime.now(timezone.utc).isoformat(timespec="seconds")
	conn = get_db_connection()
	cursor = conn.cursor()
	try:
		cursor.execute(
			"SELECT id FROM trigger_intents WHERE name = ?",
			(name,),
		)
		row = cursor.fetchone()
		if row:
			intent_id = int(row["id"])
			cursor.execute("""
				UPDATE trigger_intents
				SET description = ?, response_type = ?, answer = ?, is_active = ?,
					semantic_enabled = ?, updated_at = ?
				WHERE id = ?
			""", (description, response_type, answer, is_active, semantic_enabled, now, intent_id))
		else:
			cursor.execute("""
				INSERT INTO trigger_intents (
					name, description, response_type, answer, is_active, semantic_enabled, created_at, updated_at
				) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
			""", (name, description, response_type, answer, is_active, semantic_enabled, now, now))
			intent_id = int(cursor.lastrowid)
		conn.commit()
		return intent_id
	finally:
		conn.close()


def add_intent_phrases(intent_id: int, phrases: list[str], match_type: str = "phrase") -> int:
	now = datetime.now(timezone.utc).isoformat(timespec="seconds")
	added = 0
	conn = get_db_connection()
	cursor = conn.cursor()
	try:
		for phrase in phrases:
			cleaned = (phrase or "").strip().lower()
			if not cleaned:
				continue
			cursor.execute("""
				INSERT OR IGNORE INTO trigger_intent_phrases (
					intent_id, phrase, match_type, is_active, created_at, updated_at
				) VALUES (?, ?, ?, 1, ?, ?)
			""", (intent_id, cleaned, match_type, now, now))
			added += cursor.rowcount
		conn.commit()
		return added
	finally:
		conn.close()


def delete_trigger_intent(intent_id: int) -> bool:
	conn = get_db_connection()
	cursor = conn.cursor()
	try:
		cursor.execute("DELETE FROM trigger_intent_phrases WHERE intent_id = ?", (intent_id,))
		cursor.execute("DELETE FROM trigger_intents WHERE id = ?", (intent_id,))
		deleted = cursor.rowcount > 0
		conn.commit()
		return deleted
	finally:
		conn.close()


def get_intent_phrase(phrase_id: int) -> dict | None:
	conn = get_db_connection()
	cursor = conn.cursor()
	try:
		cursor.execute("""
			SELECT id, intent_id, phrase, match_type, is_active
			FROM trigger_intent_phrases
			WHERE id = ?
		""", (phrase_id,))
		row = cursor.fetchone()
		return dict(row) if row else None
	finally:
		conn.close()


def delete_intent_phrase(phrase_id: int) -> bool:
	conn = get_db_connection()
	cursor = conn.cursor()
	try:
		cursor.execute("DELETE FROM trigger_intent_phrases WHERE id = ?", (phrase_id,))
		deleted = cursor.rowcount > 0
		conn.commit()
		return deleted
	finally:
		conn.close()


def ensure_default_vpn_intents() -> int:
	defaults = {
		"нужен VPN": {
			"description": "Пользователь просит VPN, обход блокировок или способ открыть недоступный сервис.",
			"phrases": [
				"нужен vpn", "нужен впн", "какой vpn", "какой впн", "посоветуйте vpn",
				"посоветуйте впн", "без vpn не открывается", "без впн не открывается",
				"нужен нормальный vpn", "есть рабочий впн", "ищу vpn", "ищу впн",
				"как зайти без блокировки", "чем открыть заблокированное",
			],
		},
		"плохо работает связь": {
			"description": "Пользователь жалуется на связь, интернет, глушилки, блокировки или недоступность сервисов.",
			"phrases": [
				"связь глушат", "интернет глушат", "опять связь", "плохо ловит",
				"не работает интернет", "ютуб не открывается", "youtube не открывается",
				"инста не открывается", "telegram тупит", "телеграм тупит",
				"ничего не грузит", "сайты не открываются", "сервисы не работают",
			],
		},
		"блокировки сервисов": {
			"description": "Пользователь говорит о блокировках, ограничениях, недоступных соцсетях или медиа.",
			"phrases": [
				"заблокировали", "блокировка", "обход блокировки", "не пускает на сайт",
				"сайт заблочили", "приложение не открывается", "сервис умер",
				"опять все заблокировали", "ограничили доступ",
			],
		},
	}
	created_or_updated = 0
	for name, data in defaults.items():
		intent_id = upsert_trigger_intent(name, data["description"], "openai", "", 1, 1)
		add_intent_phrases(intent_id, data["phrases"])
		created_or_updated += 1
	return created_or_updated


def import_global_triggers_from_text(text: str, default_response_type: str = "openai") -> tuple[int, int]:
	conn = get_db_connection()
	cursor = conn.cursor()
	added = 0
	updated = 0
	try:
		for raw_line in (text or "").splitlines():
			line = raw_line.strip()
			if not line or line.startswith("#"):
				continue
			response_type = default_response_type
			answer = ""
			keyword = line
			if "=>" in line:
				keyword, answer = [part.strip() for part in line.split("=>", 1)]
				response_type = "predefined" if answer else default_response_type
			keyword = keyword.lower()
			if not keyword:
				continue
			cursor.execute(
				"SELECT 1 FROM responses WHERE keyword = ?",
				(keyword,),
			)
			exists = cursor.fetchone() is not None
			cursor.execute("""
				INSERT OR REPLACE INTO responses (keyword, answer, response_type)
				VALUES (?, ?, ?)
			""", (keyword, answer, response_type))
			if exists:
				updated += 1
			else:
				added += 1
		conn.commit()
		return added, updated
	finally:
		conn.close()


def build_full_trigger_export_text() -> str:
	lines = ["Продвинутые триггеры", ""]
	intents = fetch_trigger_intents(include_phrases=True)
	lines.append(f"Намерения: {len(intents)}")
	for intent in intents:
		status = "active" if int(intent.get("is_active") or 0) else "off"
		lines.append(f"- {intent['name']} [{status}]")
		if intent.get("description"):
			lines.append(f"  Описание: {intent['description']}")
		for phrase in intent.get("phrases", []):
			lines.append(f"  - ({phrase.get('match_type')}) {phrase.get('phrase')}")
	lines.append("")

	responses = fetch_global_responses()
	lines.append(f"Глобальные ключевые фразы: {len(responses)}")
	for row in responses:
		lines.append(f"- [{row.get('response_type')}] {row.get('keyword')}")
	return "\n".join(lines).strip() + "\n"


def semantic_analysis_enabled() -> bool:
	return _as_bool(get_config_value("trigger_semantic_enabled", "False"))


def semantic_threshold() -> float:
	return _as_float(get_config_value("trigger_semantic_threshold", "0.72"), 0.72)


def _semantic_rate_limited() -> bool:
	max_per_minute = max(0, _as_int(get_config_value("trigger_semantic_max_per_minute", "30"), 30))
	if max_per_minute <= 0:
		return True
	now = time.monotonic()
	while _semantic_rate_window and now - _semantic_rate_window[0] > 60:
		_semantic_rate_window.pop(0)
	if len(_semantic_rate_window) >= max_per_minute:
		return True
	_semantic_rate_window.append(now)
	return False


def _parse_classifier_json(content: str | None) -> dict:
	text = (content or "").strip()
	if not text:
		return {}
	try:
		return json.loads(text)
	except json.JSONDecodeError:
		match = re.search(r"\{.*\}", text, flags=re.S)
		if not match:
			return {}
		try:
			return json.loads(match.group(0))
		except json.JSONDecodeError:
			return {}


def _semantic_candidates() -> list[dict]:
	intents = fetch_trigger_intents(include_phrases=True, active_only=True)
	candidates = []
	for intent in intents:
		if not int(intent.get("semantic_enabled") or 0):
			continue
		candidates.append({
			"id": intent["id"],
			"name": intent["name"],
			"description": intent.get("description") or "",
			"examples": [phrase["phrase"] for phrase in intent.get("phrases", [])[:20]],
		})
	return candidates


def classify_semantic_intent(message_text: str | None) -> TriggerDecision:
	if not semantic_analysis_enabled():
		return TriggerDecision(reason="semantic_disabled")
	if _semantic_rate_limited():
		return TriggerDecision(reason="semantic_rate_limited")

	candidates = _semantic_candidates()
	if not candidates:
		return TriggerDecision(reason="no_semantic_intents")

	model = get_config_value("trigger_semantic_model", get_config_value("ai_trigger_model", "gpt-4o-mini"))
	max_tokens = _as_int(get_config_value("trigger_semantic_max_tokens", "180"), 180)
	messages = [
		{
			"role": "system",
			"content": (
				"Ты классификатор Telegram-сообщений. Определи, подходит ли сообщение под одно из намерений. "
				"Верни только JSON без markdown: "
				'{"matched": true/false, "intent": "название или пусто", "confidence": 0.0-1.0, "reason": "коротко"}. '
				"Если сомневаешься, matched=false."
			),
		},
		{
			"role": "user",
			"content": json.dumps(
				{
					"message": message_text or "",
					"intents": candidates,
				},
				ensure_ascii=False,
			),
		},
	]
	result = generate_ai_response(
		messages=messages,
		openai_model=model,
		provider="openai",
		temperature=0,
		max_tokens=max_tokens,
	)
	if not result.get("success"):
		return TriggerDecision(reason=f"semantic_ai_error:{result.get('content', '')[:80]}")

	payload = _parse_classifier_json(result.get("content"))
	if not payload or not payload.get("matched"):
		return TriggerDecision(reason="semantic_no_match")

	confidence = _as_float(payload.get("confidence"), 0.0)
	if confidence < semantic_threshold():
		return TriggerDecision(confidence=confidence, reason="semantic_low_confidence")

	intent_name = str(payload.get("intent") or "").strip()
	intent_by_name = {str(item["name"]): item for item in fetch_trigger_intents(include_phrases=False, active_only=True)}
	intent = intent_by_name.get(intent_name)
	if not intent:
		return TriggerDecision(confidence=confidence, reason="semantic_unknown_intent")

	return TriggerDecision(
		matched=True,
		source="semantic",
		match_type="semantic",
		intent_id=int(intent["id"]),
		intent_name=str(intent["name"]),
		response_type=str(intent.get("response_type") or "openai"),
		answer=str(intent.get("answer") or ""),
		confidence=confidence,
		reason=str(payload.get("reason") or "semantic_match"),
		semantic=True,
	)


def get_trigger_cooldown_reason(chat_id, sender_id, cooldown_key: str | None = None) -> str | None:
	now = time.monotonic()
	chat_cooldown = max(0, _as_int(get_config_value("trigger_chat_cooldown_seconds", "120"), 120))
	user_cooldown = max(0, _as_int(get_config_value("trigger_user_cooldown_seconds", "300"), 300))
	intent_cooldown = max(0, _as_int(get_config_value("trigger_intent_cooldown_seconds", "120"), 120))
	checks = [
		(("chat", str(chat_id)), chat_cooldown, "chat_cooldown"),
		(("user", str(chat_id), str(sender_id)), user_cooldown, "user_cooldown"),
	]
	if cooldown_key:
		checks.append((("intent", str(chat_id), str(cooldown_key)), intent_cooldown, "intent_cooldown"))
	for key, timeout, reason in checks:
		if timeout <= 0:
			continue
		last_at = _trigger_delivery_state.get(key)
		if last_at and now - last_at < timeout:
			return reason
	return None


def record_trigger_delivery(chat_id, sender_id, cooldown_key: str | None = None) -> None:
	now = time.monotonic()
	_trigger_delivery_state[("chat", str(chat_id))] = now
	_trigger_delivery_state[("user", str(chat_id), str(sender_id))] = now
	if cooldown_key:
		_trigger_delivery_state[("intent", str(chat_id), str(cooldown_key))] = now


def evaluate_trigger_for_message(
	message_text: str | None,
	chat_id=None,
	sender_id=None,
	category_name: str | None = None,
	allow_global: bool = True,
	allow_semantic: bool = False,
	respect_cooldown: bool = True,
) -> TriggerDecision:
	min_len = max(0, _as_int(get_config_value("trigger_min_message_length", "3"), 3))
	if len(compact_text(message_text)) < min_len:
		return TriggerDecision(reason="message_too_short")

	if respect_cooldown and chat_id is not None and sender_id is not None:
		reason = get_trigger_cooldown_reason(chat_id, sender_id)
		if reason:
			return TriggerDecision(reason=reason)

	category_rows = fetch_category_keywords(category_name)
	global_rows = fetch_global_responses() if allow_global else []
	intent_rows = fetch_intent_phrase_rows() if allow_global else []
	decision = best_local_trigger_match(
		message_text,
		category_keywords=category_rows,
		global_responses=global_rows,
		intent_phrase_rows=intent_rows,
		category_name=category_name,
	)
	if decision.matched:
		if respect_cooldown and chat_id is not None and sender_id is not None:
			reason = get_trigger_cooldown_reason(chat_id, sender_id, decision.cooldown_key)
			if reason:
				return TriggerDecision(reason=reason)
		return decision

	if allow_semantic:
		semantic_decision = classify_semantic_intent(message_text)
		if semantic_decision.matched and respect_cooldown and chat_id is not None and sender_id is not None:
			reason = get_trigger_cooldown_reason(chat_id, sender_id, semantic_decision.cooldown_key)
			if reason:
				return TriggerDecision(reason=reason)
		return semantic_decision

	return decision


def set_trigger_semantic_enabled(enabled: bool) -> None:
	set_config_value("trigger_semantic_enabled", "True" if enabled else "False")
