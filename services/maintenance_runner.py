from datetime import datetime, timezone

from db import get_config_value, record_health_check_run, set_config_value
from services.account_health import run_account_health_check
from services.proxy_repository import refresh_expired_proxies
from services.proxy_health import run_proxy_health_check


def _utc_now() -> datetime:
	return datetime.now(timezone.utc)


def _parse_dt(value: str | None) -> datetime | None:
	if not value:
		return None
	try:
		parsed = datetime.fromisoformat(value)
		if parsed.tzinfo is None:
			parsed = parsed.replace(tzinfo=timezone.utc)
		return parsed.astimezone(timezone.utc)
	except ValueError:
		return None


def _int_config(key: str, default: int, min_value: int = 1) -> int:
	try:
		return max(int(get_config_value(key, str(default))), min_value)
	except (TypeError, ValueError):
		return default


def _check_due(last_key: str, interval_key: str, default_minutes: int) -> tuple[bool, str]:
	now = _utc_now()
	last_value = get_config_value(last_key)
	if not last_value:
		set_config_value(last_key, now.isoformat(timespec="seconds"))
		return False, "initialized"

	last_dt = _parse_dt(last_value)
	interval_minutes = _int_config(interval_key, default_minutes, 5)
	if not last_dt:
		return True, "bad_last_timestamp"
	elapsed = (now - last_dt).total_seconds() / 60
	return elapsed >= interval_minutes, f"{int(elapsed)}/{interval_minutes}m"


async def run_scheduled_maintenance_for_current_workspace() -> dict:
	from userbot import restart_workspace_clients

	summary = {"expired_accounts": 0, "proxy_check": None, "account_check": None}
	expired_account_ids = refresh_expired_proxies()
	if expired_account_ids:
		summary["expired_accounts"] = len(expired_account_ids)
		await restart_workspace_clients()

	if get_config_value("maintenance_auto_checks_enabled", "1") != "1":
		return summary

	proxy_due, _ = _check_due(
		"maintenance_last_proxy_check_at",
		"proxy_auto_check_interval_minutes",
		360
	)
	if proxy_due:
		started_at = _utc_now().isoformat(timespec="seconds")
		proxy_result = await run_proxy_health_check(concurrency=5)
		set_config_value("maintenance_last_proxy_check_at", _utc_now().isoformat(timespec="seconds"))
		if proxy_result.get("affected_accounts"):
			await restart_workspace_clients()
		record_health_check_run(
			"proxy",
			started_at,
			"ok",
			f"active={proxy_result['stats']['active']}, failed={proxy_result['stats']['failed']}, expired={proxy_result['stats']['expired']}"
		)
		summary["proxy_check"] = proxy_result

	account_due, _ = _check_due(
		"maintenance_last_account_check_at",
		"account_auto_check_interval_minutes",
		720
	)
	if account_due:
		started_at = _utc_now().isoformat(timespec="seconds")
		account_result = await run_account_health_check(include_write_probe=False)
		set_config_value("maintenance_last_account_check_at", _utc_now().isoformat(timespec="seconds"))
		record_health_check_run(
			"accounts",
			started_at,
			"ok",
			f"ok={account_result['stats']['ok']}, warning={account_result['stats']['warning']}, bad={account_result['stats']['bad']}, disabled={account_result['stats']['disabled']}"
		)
		summary["account_check"] = account_result

	return summary
