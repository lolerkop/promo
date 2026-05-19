import html
from datetime import datetime, timezone

from db import (count_bot_errors, get_account_health_summary,
                get_active_workspace_name, get_config_value, get_db_connection,
                get_proxy_summary, list_account_runtime_state,
                list_health_check_runs)


def _table_count(table_name: str, where: str = "", params: tuple = ()) -> int:
	conn = get_db_connection()
	c = conn.cursor()
	query = f"SELECT COUNT(*) AS cnt FROM {table_name}"
	if where:
		query += f" WHERE {where}"
	c.execute(query, params)
	count = c.fetchone()["cnt"]
	conn.close()
	return count


def build_health_snapshot() -> dict:
	from userbot import get_active_clients

	active_clients = [(client, data) for client, data in get_active_clients() if client.is_connected()]
	proxy_summary = get_proxy_summary()
	account_health = get_account_health_summary()

	return {
		"workspace_name": get_active_workspace_name(),
		"accounts_total": _table_count("accounts"),
		"accounts_enabled": _table_count("accounts", "COALESCE(is_enabled, 1) = 1"),
		"accounts_disabled": _table_count("accounts", "COALESCE(is_enabled, 1) = 0"),
		"clients_running": len(active_clients),
		"channels_total": _table_count("channels"),
		"channels_enabled": _table_count("channels", "enabled = 1"),
		"groups_total": _table_count("groups"),
		"groups_enabled": _table_count("groups", "enabled = 1"),
		"proxies": proxy_summary,
		"account_health": account_health,
		"errors_total": count_bot_errors(),
		"auto_checks_enabled": get_config_value("maintenance_auto_checks_enabled", "1") == "1",
		"proxy_interval": get_config_value("proxy_auto_check_interval_minutes", "360"),
		"account_interval": get_config_value("account_auto_check_interval_minutes", "720"),
		"last_runs": list_health_check_runs(5),
		"runtime_state": list_account_runtime_state(8),
		"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
	}


def format_health_dashboard(snapshot: dict) -> str:
	proxies = snapshot["proxies"]
	account_health = snapshot["account_health"]
	auto_checks = "включены" if snapshot["auto_checks_enabled"] else "выключены"

	lines = [
		f"<b>Центр здоровья</b>",
		f"Пространство: <b>{html.escape(snapshot['workspace_name'])}</b>",
		"",
		"<b>Аккаунты</b>",
		f"Всего: <b>{snapshot['accounts_total']}</b>",
		f"Включены: <b>{snapshot['accounts_enabled']}</b>",
		f"Выключены: <b>{snapshot['accounts_disabled']}</b>",
		f"Запущено клиентов: <b>{snapshot['clients_running']}</b>",
		f"Health OK/Warn/Bad: <b>{account_health.get('ok', 0)}</b> / <b>{account_health.get('warning', 0)}</b> / <b>{account_health.get('bad', 0)}</b>",
		"",
		"<b>Прокси</b>",
		f"Всего: <b>{proxies['total']}</b>",
		f"Свободно/занято: <b>{proxies['free']}</b> / <b>{proxies['busy']}</b>",
		f"Не работают/истекли: <b>{proxies['failed']}</b> / <b>{proxies['expired']}</b>",
		"",
		"<b>Каналы и группы</b>",
		f"Каналы включено/всего: <b>{snapshot['channels_enabled']}</b> / <b>{snapshot['channels_total']}</b>",
		f"Группы включено/всего: <b>{snapshot['groups_enabled']}</b> / <b>{snapshot['groups_total']}</b>",
		"",
		"<b>Обслуживание</b>",
		f"Автопроверки: <b>{auto_checks}</b>",
		f"Прокси: раз в <b>{html.escape(str(snapshot['proxy_interval']))}</b> мин",
		f"Сессии: раз в <b>{html.escape(str(snapshot['account_interval']))}</b> мин",
		f"Ошибок в журнале: <b>{snapshot['errors_total']}</b>",
	]

	if snapshot["last_runs"]:
		lines.extend(["", "<b>Последние проверки</b>"])
		for run in snapshot["last_runs"]:
			lines.append(
				f"- {html.escape(str(run['check_type']))}: "
				f"{html.escape(str(run.get('status') or '-'))} "
				f"<code>{html.escape(str(run.get('finished_at') or run.get('started_at') or '-'))}</code>"
			)

	if snapshot["runtime_state"]:
		lines.extend(["", "<b>Последние действия аккаунтов</b>"])
		for state in snapshot["runtime_state"][:5]:
			label = state.get("label") or state.get("session_name") or f"ID {state.get('account_id')}"
			lines.append(
				f"- {html.escape(str(label))}: {html.escape(str(state.get('action_type')))} "
				f"ok/fail {state.get('successes_today', 0)}/{state.get('failures_today', 0)}"
			)

	return "\n".join(lines)

