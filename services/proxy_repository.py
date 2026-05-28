import logging
import sqlite3
from datetime import datetime, timezone

from db import get_db_connection


def _parse_proxy_datetime(value: str | None) -> datetime | None:
	if not value:
		return None
	value = str(value).strip()
	if not value:
		return None
	if len(value) == 10:
		value = f"{value} 23:59:59"
	if value.endswith("Z"):
		value = value[:-1] + "+00:00"
	for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
		try:
			return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
		except ValueError:
			pass
	try:
		parsed = datetime.fromisoformat(value)
		if parsed.tzinfo is None:
			parsed = parsed.replace(tzinfo=timezone.utc)
		return parsed.astimezone(timezone.utc)
	except ValueError:
		return None


def _proxy_is_expired(proxy_row: dict) -> bool:
	status = (proxy_row.get("status") or "active").lower()
	if status == "expired":
		return True
	expires_at = _parse_proxy_datetime(proxy_row.get("expires_at"))
	return bool(expires_at and expires_at <= datetime.now(timezone.utc))


def _proxy_status_label(proxy_row: dict) -> str:
	return "expired" if _proxy_is_expired(proxy_row) else (proxy_row.get("status") or "active")


def _clear_account_proxy(cursor, account_id: int):
	cursor.execute("""
		UPDATE accounts
		SET proxy_type = NULL,
			proxy_ip = NULL,
			proxy_port = NULL,
			proxy_username = NULL,
			proxy_password = NULL
		WHERE id = ?
	""", (account_id,))


def get_proxy_summary() -> dict:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("SELECT * FROM proxies")
	rows = [dict(row) for row in c.fetchall()]
	conn.close()
	total = len(rows)
	expired = sum(1 for row in rows if _proxy_is_expired(row))
	failed = sum(1 for row in rows if not _proxy_is_expired(row) and (row.get("status") or "active").lower() == "failed")
	busy = sum(
		1 for row in rows
		if row.get("assigned_account_id") is not None
		and not _proxy_is_expired(row)
		and (row.get("status") or "active").lower() == "active"
	)
	free = sum(
		1 for row in rows
		if row.get("assigned_account_id") is None
		and not _proxy_is_expired(row)
		and (row.get("status") or "active").lower() == "active"
	)
	return {"total": total, "free": free, "busy": busy, "expired": expired, "failed": failed}


def refresh_expired_proxies() -> list[int]:
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("""
			SELECT id, assigned_account_id, expires_at
			FROM proxies
			WHERE expires_at IS NOT NULL
			  AND expires_at != ''
			  AND COALESCE(status, 'active') != 'expired'
		""")
		expired_rows = []
		for row in c.fetchall():
			row_dict = dict(row)
			expires_at = _parse_proxy_datetime(row_dict.get("expires_at"))
			if expires_at and expires_at <= datetime.now(timezone.utc):
				expired_rows.append(row_dict)

		for proxy in expired_rows:
			c.execute("""
				UPDATE proxies
				SET status = 'expired',
					last_checked_at = ?,
					last_error = ?
				WHERE id = ?
			""", (
				datetime.now(timezone.utc).isoformat(timespec="seconds"),
				"Rental expiration time has passed.",
				proxy["id"]
			))
		conn.commit()
		return [
			row["assigned_account_id"]
			for row in expired_rows
			if row.get("assigned_account_id") is not None
		]
	except Exception as e:
		logging.error(f"Error refreshing expired proxies: {e}")
		conn.rollback()
		return []
	finally:
		conn.close()


def update_proxy_check_result(proxy_id: int, status: str, last_error: str = None):
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("""
			UPDATE proxies
			SET status = ?,
				last_checked_at = ?,
				last_error = ?
			WHERE id = ?
		""", (
			status,
			datetime.now(timezone.utc).isoformat(timespec="seconds"),
			last_error,
			proxy_id
		))
		conn.commit()
	except Exception as e:
		logging.error(f"Error updating proxy check result {proxy_id}: {e}")
		conn.rollback()
	finally:
		conn.close()


def get_all_proxies_for_check() -> list[dict]:
	refresh_expired_proxies()
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
		SELECT p.*,
			   a.label AS account_label,
			   a.session_name AS account_session_name,
			   a.phone AS account_phone
		FROM proxies p
		LEFT JOIN accounts a ON a.id = p.assigned_account_id
		ORDER BY p.id
	""")
	rows = []
	for row in c.fetchall():
		item = dict(row)
		item["computed_status"] = _proxy_status_label(item)
		rows.append(item)
	conn.close()
	return rows


def get_account_proxy_block_reason(account_data: dict) -> str | None:
	if not account_data.get("proxy_ip") or not account_data.get("proxy_port"):
		return None

	refresh_expired_proxies()
	conn = get_db_connection()
	c = conn.cursor()
	row = None
	try:
		c.execute("SELECT * FROM proxies WHERE assigned_account_id = ? LIMIT 1", (account_data.get("id"),))
		row = c.fetchone()
		if not row:
			c.execute(
				"SELECT * FROM proxies WHERE proxy_ip = ? AND proxy_port = ? LIMIT 1",
				(account_data.get("proxy_ip"), account_data.get("proxy_port"))
			)
			row = c.fetchone()
	finally:
		conn.close()

	if not row:
		return None

	proxy = dict(row)
	proxy_addr = f"{proxy.get('proxy_ip')}:{proxy.get('proxy_port')}"
	if _proxy_is_expired(proxy):
		return f"proxy {proxy_addr} is expired"

	status = (proxy.get("status") or "active").lower()
	if status in {"failed", "disabled", "expired"}:
		return f"proxy {proxy_addr} status is {status}"

	return None


def list_proxies(page: int = 1, per_page: int = 8) -> tuple[list[dict], int, int]:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("SELECT COUNT(*) AS cnt FROM proxies")
	total = c.fetchone()["cnt"]
	total_pages = (total + per_page - 1) // per_page or 1
	page = max(1, min(page, total_pages))
	offset = (page - 1) * per_page
	c.execute("""
		SELECT p.*,
			   a.label AS account_label,
			   a.session_name AS account_session_name,
			   a.phone AS account_phone
		FROM proxies p
		LEFT JOIN accounts a ON a.id = p.assigned_account_id
		ORDER BY p.id
		LIMIT ? OFFSET ?
	""", (per_page, offset))
	rows = []
	for row in c.fetchall():
		item = dict(row)
		item["computed_status"] = _proxy_status_label(item)
		rows.append(item)
	conn.close()
	return rows, page, total_pages


def get_proxy_details(proxy_id: int) -> dict | None:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
		SELECT p.*,
			   a.label AS account_label,
			   a.session_name AS account_session_name,
			   a.phone AS account_phone
		FROM proxies p
		LEFT JOIN accounts a ON a.id = p.assigned_account_id
		WHERE p.id = ?
	""", (proxy_id,))
	row = c.fetchone()
	conn.close()
	if not row:
		return None
	item = dict(row)
	item["computed_status"] = _proxy_status_label(item)
	return item


def delete_proxy(proxy_id: int) -> bool:
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("SELECT assigned_account_id FROM proxies WHERE id = ?", (proxy_id,))
		row = c.fetchone()
		if not row:
			return False
		if row["assigned_account_id"] is not None:
			_clear_account_proxy(c, row["assigned_account_id"])
		c.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
		conn.commit()
		return True
	except Exception as e:
		logging.error(f"Error deleting proxy {proxy_id}: {e}")
		conn.rollback()
		return False
	finally:
		conn.close()


def delete_expired_proxies() -> int:
	conn = get_db_connection()
	c = conn.cursor()
	deleted_count = 0
	try:
		c.execute("SELECT * FROM proxies")
		expired_rows = [dict(row) for row in c.fetchall() if _proxy_is_expired(dict(row))]
		for proxy in expired_rows:
			if proxy.get("assigned_account_id") is not None:
				_clear_account_proxy(c, proxy["assigned_account_id"])
			c.execute("DELETE FROM proxies WHERE id = ?", (proxy["id"],))
			deleted_count += c.rowcount
		conn.commit()
		return deleted_count
	except Exception as e:
		logging.error(f"Error deleting expired proxies: {e}")
		conn.rollback()
		return 0
	finally:
		conn.close()


def delete_all_proxies() -> int:
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("SELECT COUNT(*) AS cnt FROM proxies")
		total = c.fetchone()["cnt"]
		c.execute("""
			UPDATE accounts
			SET proxy_type = NULL,
				proxy_ip = NULL,
				proxy_port = NULL,
				proxy_username = NULL,
				proxy_password = NULL
		""")
		c.execute("DELETE FROM proxies")
		conn.commit()
		return total
	except Exception as e:
		logging.error(f"Error deleting all proxies: {e}")
		conn.rollback()
		return 0
	finally:
		conn.close()


def add_proxies_bulk(proxies: list[dict]) -> tuple[int, int]:
	conn = get_db_connection()
	c = conn.cursor()
	added_count = 0
	skipped_count = 0
	for proxy in proxies:
		try:
			expires_at = proxy.get('expires_at')
			if expires_at and len(str(expires_at).strip()) == 10:
				expires_at = f"{str(expires_at).strip()} 23:59:59"
			parsed_expires_at = _parse_proxy_datetime(expires_at)
			if expires_at and not parsed_expires_at:
				logging.warning(f"Ignoring invalid proxy expiration value for {proxy.get('proxy_ip')}:{proxy.get('proxy_port')}: {expires_at}")
				expires_at = None
			status = proxy.get('status') or 'active'
			if parsed_expires_at and parsed_expires_at <= datetime.now(timezone.utc):
				status = 'expired'
			c.execute("""
				INSERT INTO proxies (proxy_type, proxy_ip, proxy_port, proxy_username, proxy_password, status, expires_at)
				VALUES (?, ?, ?, ?, ?, ?, ?)
			""", (
				proxy.get('proxy_type', 'socks5'),
				proxy['proxy_ip'],
				proxy['proxy_port'],
				proxy.get('proxy_username'),
				proxy.get('proxy_password'),
				status,
				expires_at
			))
			added_count += 1
		except sqlite3.IntegrityError:
			skipped_count += 1
		except Exception as e:
			logging.error(f"Error inserting proxy {proxy.get('proxy_ip')}: {e}")
			skipped_count += 1
	conn.commit()
	conn.close()
	return added_count, skipped_count


def get_unassigned_proxy() -> dict | None:
	refresh_expired_proxies()
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
		SELECT *
		FROM proxies
		WHERE assigned_account_id IS NULL
		  AND COALESCE(status, 'active') = 'active'
		ORDER BY RANDOM()
	""")
	rows = [dict(row) for row in c.fetchall()]
	conn.close()
	for row in rows:
		if not _proxy_is_expired(row):
			return row
	return None


def assign_proxy_to_account(proxy_id: int, account_id: int):
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("UPDATE proxies SET assigned_account_id = ? WHERE id = ?", (account_id, proxy_id))
		conn.commit()
		logging.info(f"Proxy {proxy_id} assigned to account {account_id}")
	except Exception as e:
		logging.error(f"Error assigning proxy {proxy_id} to account {account_id}: {e}")
		conn.rollback()
	finally:
		conn.close()


def upsert_proxy_for_account(account_id: int, proxy_details: dict):
	if not proxy_details.get("proxy_ip") or not proxy_details.get("proxy_port"):
		return
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute(
			"SELECT id FROM proxies WHERE proxy_ip = ? AND proxy_port = ?",
			(proxy_details.get("proxy_ip"), proxy_details.get("proxy_port"))
		)
		row = c.fetchone()
		if row:
			c.execute("""
				UPDATE proxies
				SET proxy_type = ?,
					proxy_username = ?,
					proxy_password = ?,
					status = 'active',
					last_checked_at = NULL,
					last_error = NULL,
					assigned_account_id = ?
				WHERE id = ?
			""", (
				proxy_details.get("proxy_type") or "socks5",
				proxy_details.get("proxy_username"),
				proxy_details.get("proxy_password"),
				account_id,
				row["id"]
			))
		else:
			c.execute("""
				INSERT INTO proxies (
					proxy_type, proxy_ip, proxy_port, proxy_username, proxy_password,
					assigned_account_id, status
				)
				VALUES (?, ?, ?, ?, ?, ?, 'active')
			""", (
				proxy_details.get("proxy_type") or "socks5",
				proxy_details.get("proxy_ip"),
				proxy_details.get("proxy_port"),
				proxy_details.get("proxy_username"),
				proxy_details.get("proxy_password"),
				account_id
			))
		conn.commit()
	except Exception as e:
		logging.error(f"Error upserting proxy for account {account_id}: {e}")
		conn.rollback()
	finally:
		conn.close()
