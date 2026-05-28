import logging
from datetime import datetime, timezone

from db import get_db_connection

VALID_ENTITY_TYPES = {"channel", "group"}


def _now_iso() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_entity_type(entity_type: str) -> str:
	entity_type = (entity_type or "").strip().lower()
	if entity_type not in VALID_ENTITY_TYPES:
		raise ValueError(f"Unknown entity type: {entity_type}")
	return entity_type


def _normalize_account_ids(account_ids: list[int]) -> list[int]:
	normalized = []
	seen = set()
	for account_id in account_ids or []:
		try:
			account_id = int(account_id)
		except (TypeError, ValueError):
			continue
		if account_id in seen:
			continue
		seen.add(account_id)
		normalized.append(account_id)
	return normalized


def record_entity_memberships(account_ids: list[int], entity_type: str, entity_id: int, identifier: str = None) -> int:
	entity_type = _normalize_entity_type(entity_type)
	account_ids = _normalize_account_ids(account_ids)
	if not account_ids:
		return 0

	now = _now_iso()
	conn = get_db_connection()
	c = conn.cursor()
	try:
		for account_id in account_ids:
			c.execute("""
				INSERT INTO account_entity_memberships (
					account_id, entity_type, entity_id, identifier, status, joined_at, updated_at
				)
				VALUES (?, ?, ?, ?, 'active', ?, ?)
				ON CONFLICT(account_id, entity_type, entity_id) DO UPDATE SET
					identifier = COALESCE(excluded.identifier, account_entity_memberships.identifier),
					status = 'active',
					updated_at = excluded.updated_at
			""", (account_id, entity_type, int(entity_id), identifier, now, now))
		conn.commit()
		return len(account_ids)
	except Exception as e:
		conn.rollback()
		logging.warning(f"Could not record subscription memberships for {entity_type} {entity_id}: {e}")
		return 0
	finally:
		conn.close()


def mark_entity_memberships_left(entity_type: str, entity_ids: list[int], account_ids: list[int] = None) -> int:
	entity_type = _normalize_entity_type(entity_type)
	entity_ids = [int(entity_id) for entity_id in entity_ids or [] if entity_id is not None]
	if not entity_ids:
		return 0

	account_ids = _normalize_account_ids(account_ids) if account_ids is not None else None
	now = _now_iso()
	conn = get_db_connection()
	c = conn.cursor()
	try:
		entity_placeholders = ",".join("?" for _ in entity_ids)
		params = [now, entity_type, *entity_ids]
		account_filter = ""
		if account_ids:
			account_filter = f" AND account_id IN ({','.join('?' for _ in account_ids)})"
			params.extend(account_ids)
		c.execute(f"""
			UPDATE account_entity_memberships
			SET status = 'left', updated_at = ?
			WHERE entity_type = ?
			  AND entity_id IN ({entity_placeholders})
			  {account_filter}
		""", params)
		conn.commit()
		return c.rowcount
	except Exception as e:
		conn.rollback()
		logging.warning(f"Could not mark subscription memberships left for {entity_type}: {e}")
		return 0
	finally:
		conn.close()


def get_account_subscription_loads() -> dict[int, int]:
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("""
			SELECT account_id, COUNT(*) AS total_count
			FROM account_entity_memberships
			WHERE status = 'active'
			GROUP BY account_id
		""")
		return {int(row["account_id"]): int(row["total_count"] or 0) for row in c.fetchall()}
	except Exception as e:
		logging.warning(f"Could not load persisted subscription memberships, using assigned entities fallback: {e}")
		return _get_assigned_entity_loads(c)
	finally:
		conn.close()


def _get_assigned_entity_loads(cursor) -> dict[int, int]:
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
	return {int(row["account_id"]): int(row["total_count"] or 0) for row in cursor.fetchall()}
