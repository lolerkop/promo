import asyncio
import html
import os
import random

from telethon import TelegramClient
from telethon.errors import (AuthKeyDuplicatedError, AuthKeyInvalidError,
                             AuthKeyUnregisteredError, FloodWaitError,
                             PhoneNumberBannedError, RPCError,
                             SessionRevokedError, UserDeactivatedBanError,
                             UserDeactivatedError, UserRestrictedError)
from telethon.sessions import SQLiteSession
from telethon.tl.functions.updates import GetStateRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import InputUserSelf

from db import (get_account_proxy_block_reason, get_db_connection,
                get_workspace_session_dir, record_account_runtime_event,
                update_account_health_status)


FATAL_SESSION_ERRORS = (
	AuthKeyDuplicatedError,
	AuthKeyInvalidError,
	AuthKeyUnregisteredError,
	PhoneNumberBannedError,
	SessionRevokedError,
	UserDeactivatedBanError,
	UserDeactivatedError,
)


def build_proxy_params(account_data: dict):
	if not account_data.get("proxy_ip") or not account_data.get("proxy_port"):
		return None
	proxy = {
		"proxy_type": (account_data.get("proxy_type") or "socks5").lower(),
		"addr": account_data.get("proxy_ip"),
		"port": int(account_data.get("proxy_port")),
	}
	if account_data.get("proxy_username"):
		proxy["username"] = account_data.get("proxy_username")
	if account_data.get("proxy_password"):
		proxy["password"] = account_data.get("proxy_password")
	return proxy


async def _run_health_probe(client: TelegramClient, account_data: dict, include_write_probe: bool = False):
	warnings = []
	details = []

	if not client.is_connected():
		await client.connect()
	if not client.is_connected():
		return "bad", "Could not connect to Telegram.", None, False

	if not await client.is_user_authorized():
		return "bad", "Session is not authorized or was revoked.", None, False

	me = await client.get_me()
	if not me:
		return "bad", "Authorized, but get_me returned no user data.", None, True

	full_name = f"{getattr(me, 'first_name', '') or ''} {getattr(me, 'last_name', '') or ''}".strip()
	details.append(f"Telegram ID: {me.id}")
	if full_name:
		details.append(f"Name: {full_name}")
	username = getattr(me, "username", None)
	if username:
		details.append(f"Username: @{username}")

	if getattr(me, "deleted", False):
		return "bad", "Account user object is marked as deleted/deactivated.", me.id, True
	if getattr(me, "restricted", False):
		warnings.append("User object has restricted=True.")
	restriction_reason = getattr(me, "restriction_reason", None)
	if restriction_reason:
		warnings.append(f"Restriction reason present: {str(restriction_reason)[:250]}")
	if getattr(me, "scam", False):
		warnings.append("Telegram marks this account as scam.")
	if getattr(me, "fake", False):
		warnings.append("Telegram marks this account as fake.")

	await client(GetStateRequest())
	await client(GetFullUserRequest(InputUserSelf()))

	if include_write_probe:
		try:
			probe_message = await client.send_message(
				"me",
				"session health check",
				silent=True,
				link_preview=False
			)
			try:
				await client.delete_messages("me", [probe_message.id])
			except Exception as delete_err:
				warnings.append(f"Self-test message sent, but delete failed: {type(delete_err).__name__}.")
			details.append("Self-send probe: OK")
		except (UserRestrictedError, RPCError) as e_write:
			warnings.append(f"Write probe failed: {type(e_write).__name__}.")

	db_user_id = account_data.get("user_id")
	db_id = account_data.get("id")
	if db_user_id != me.id:
		details.append(f"DB UID mismatch: {db_user_id} -> {me.id}. Updated.")
		conn_uid_update = get_db_connection()
		cursor_uid_update = conn_uid_update.cursor()
		try:
			cursor_uid_update.execute("UPDATE accounts SET user_id = ? WHERE id = ?", (me.id, db_id))
			conn_uid_update.commit()
		finally:
			conn_uid_update.close()

	if warnings:
		return "warning", "\n".join(details + ["Warnings:"] + [f"- {w}" for w in warnings]), me.id, True
	return "ok", "\n".join(details), me.id, True


async def run_account_health_check(progress_callback=None, include_write_probe: bool = False) -> dict:
	from userbot import get_active_clients

	conn = get_db_connection()
	cursor = conn.cursor()
	cursor.execute("""
		SELECT id, session_name, phone, api_id, api_hash, label, user_id,
			   proxy_type, proxy_ip, proxy_port, proxy_username, proxy_password, is_enabled
		FROM accounts
		ORDER BY id
	""")
	accounts = [dict(row) for row in cursor.fetchall()]
	conn.close()

	active_clients_by_id = {
		data.get("id"): (client, data)
		for client, data in get_active_clients()
		if data and data.get("id") is not None
	}
	total = len(accounts)
	stats = {"ok": 0, "warning": 0, "bad": 0, "disabled": 0}
	results = []

	async def notify(current_label: str = ""):
		if progress_callback:
			await progress_callback({
				"total": total,
				"completed": len(results),
				"stats": dict(stats),
				"current": current_label,
				"results": list(results),
			})

	await notify()

	for account_data in accounts:
		account_id = account_data.get("id")
		label = account_data.get("label") or account_data.get("session_name") or f"ID {account_id}"
		await notify(str(label))
		status = "bad"
		details = "Not checked."
		tg_user_id = None
		authorized = False
		temp_client = None

		try:
			if not int(account_data.get("is_enabled", 1) or 0):
				status = "disabled"
				details = "Account is disabled in bot settings."
			else:
				proxy_block_reason = get_account_proxy_block_reason(account_data)
				if proxy_block_reason:
					status = "bad"
					details = f"Blocked by proxy state: {proxy_block_reason}"
				else:
					runtime_pair = active_clients_by_id.get(account_id)
					if runtime_pair:
						client = runtime_pair[0]
					else:
						session_name = account_data.get("session_name")
						if not session_name or not account_data.get("api_id") or not account_data.get("api_hash"):
							raise ValueError("Missing session_name/api_id/api_hash in DB.")
						temp_client = TelegramClient(
							SQLiteSession(os.path.join(get_workspace_session_dir(), session_name)),
							int(account_data.get("api_id")),
							account_data.get("api_hash"),
							proxy=build_proxy_params(account_data)
						)
						client = temp_client

					status, details, tg_user_id, authorized = await asyncio.wait_for(
						_run_health_probe(client, account_data, include_write_probe=include_write_probe),
						timeout=45
					)

		except FATAL_SESSION_ERRORS as e_fatal:
			status = "bad"
			details = f"Fatal session/account error: {type(e_fatal).__name__}. Account is likely banned, deactivated, or session was revoked."
		except FloodWaitError as e_flood:
			status = "warning"
			details = f"FloodWait during health check: {e_flood.seconds}s."
		except asyncio.TimeoutError:
			status = "bad"
			details = "Health check timed out."
		except Exception as e:
			status = "bad"
			details = f"Check failed: {type(e).__name__}: {str(e)[:250]}"
		finally:
			if temp_client and temp_client.is_connected():
				try:
					await temp_client.disconnect()
				except Exception:
					pass

		stats[status] += 1
		update_account_health_status(
			account_id=account_id,
			status=status,
			details=details,
			telegram_user_id=tg_user_id,
			is_authorized=authorized
		)
		if status != "disabled":
			record_account_runtime_event(
				account_id=account_id,
				action_type="session_health",
				success=status in {"ok", "warning"},
				last_error=None if status in {"ok", "warning"} else details
			)
		results.append({
			"account_id": account_id,
			"label": str(label),
			"status": status,
			"details": details,
		})
		await notify(str(label))
		await asyncio.sleep(random.uniform(0.1, 0.3))

	return {"total": total, "stats": stats, "results": results}


def format_account_health_result(result: dict) -> str:
	label = html.escape(str(result.get("label") or result.get("account_id")))
	account_id = html.escape(str(result.get("account_id") or "-"))
	status = str(result.get("status") or "unknown")
	status_label = {
		"ok": "OK",
		"warning": "WARNING",
		"bad": "BAD",
		"disabled": "DISABLED",
	}.get(status, status.upper())
	raw_details = str(result.get("details") or "-")
	if len(raw_details) > 320:
		raw_details = raw_details[:317] + "..."
	details = html.escape(raw_details)
	return (
		f"<b>{label}</b> | ID: <code>{account_id}</code>\n"
		f"Status: <b>{status_label}</b>\n"
		f"Details: <code>{details}</code>"
	)
