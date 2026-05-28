import logging
from datetime import datetime, timezone

from db import get_db_connection

DEFAULT_MAX_ACCOUNTS_PER_API = 10


def _now_iso() -> str:
	return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add_api_credential(api_id: int, api_hash: str, label: str = None, max_accounts: int = DEFAULT_MAX_ACCOUNTS_PER_API) -> int:
	api_id = int(api_id)
	api_hash = (api_hash or "").strip()
	if not api_hash:
		raise ValueError("api_hash is empty")
	max_accounts = max(1, int(max_accounts or DEFAULT_MAX_ACCOUNTS_PER_API))
	now = _now_iso()
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("""
			INSERT INTO telegram_api_credentials (
				api_id, api_hash, label, max_accounts, is_active, created_at, updated_at
			)
			VALUES (?, ?, ?, ?, 1, ?, ?)
		""", (api_id, api_hash, label or f"API {api_id}", max_accounts, now, now))
		conn.commit()
		return c.lastrowid
	except Exception:
		conn.rollback()
		raise
	finally:
		conn.close()


def list_api_credentials() -> list[dict]:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
		SELECT
			cred.id,
			cred.api_id,
			cred.api_hash,
			cred.label,
			cred.max_accounts,
			cred.is_active,
			COUNT(acc.id) AS accounts_count
		FROM telegram_api_credentials cred
		LEFT JOIN accounts acc
			ON acc.api_id = cred.api_id
		   AND acc.api_hash = cred.api_hash
		GROUP BY cred.id
		ORDER BY cred.is_active DESC, accounts_count ASC, cred.id ASC
	""")
	rows = [dict(row) for row in c.fetchall()]
	conn.close()
	return rows


def get_available_api_credential() -> dict | None:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
		SELECT
			cred.id,
			cred.api_id,
			cred.api_hash,
			cred.label,
			cred.max_accounts,
			COUNT(acc.id) AS accounts_count
		FROM telegram_api_credentials cred
		LEFT JOIN accounts acc
			ON acc.api_id = cred.api_id
		   AND acc.api_hash = cred.api_hash
		WHERE cred.is_active = 1
		GROUP BY cred.id
		HAVING accounts_count < COALESCE(cred.max_accounts, ?)
		ORDER BY accounts_count ASC, cred.id ASC
		LIMIT 1
	""", (DEFAULT_MAX_ACCOUNTS_PER_API,))
	row = c.fetchone()
	conn.close()
	return dict(row) if row else None


def set_api_credential_status(credential_id: int, is_active: bool) -> bool:
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("""
			UPDATE telegram_api_credentials
			SET is_active = ?, updated_at = ?
			WHERE id = ?
		""", (1 if is_active else 0, _now_iso(), int(credential_id)))
		conn.commit()
		return c.rowcount > 0
	except Exception as e:
		conn.rollback()
		logging.warning(f"Could not update API credential status {credential_id}: {e}")
		return False
	finally:
		conn.close()


def delete_api_credential(credential_id: int) -> tuple[bool, str]:
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("""
			SELECT cred.api_id, cred.api_hash, COUNT(acc.id) AS accounts_count
			FROM telegram_api_credentials cred
			LEFT JOIN accounts acc
				ON acc.api_id = cred.api_id
			   AND acc.api_hash = cred.api_hash
			WHERE cred.id = ?
			GROUP BY cred.id
		""", (int(credential_id),))
		row = c.fetchone()
		if not row:
			return False, "API pair not found."
		if int(row["accounts_count"] or 0) > 0:
			return False, "API pair is used by existing accounts. Disable it instead."
		c.execute("DELETE FROM telegram_api_credentials WHERE id = ?", (int(credential_id),))
		conn.commit()
		return True, "API pair deleted."
	except Exception as e:
		conn.rollback()
		return False, str(e)
	finally:
		conn.close()
