import logging

from db import get_db_connection


def connected_clients(runtime_clients: list[tuple[object, dict]]) -> list[tuple[object, dict]]:
	return [
		(client, data)
		for client, data in runtime_clients
		if client and hasattr(client, "is_connected") and client.is_connected()
	]


def get_next_account_in_cycle(cycle_name: str, runtime_clients: list[tuple[object, dict]]):
	active_clients = connected_clients(runtime_clients)
	total_active_clients = len(active_clients)
	if total_active_clients == 0:
		logging.warning(f"get_next_account_in_cycle ({cycle_name}): no connected clients")
		return (None, None)

	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("SELECT last_account_index FROM cycle_state WHERE cycle_name = ?", (cycle_name,))
		row = c.fetchone()
		last_index = row["last_account_index"] if row else -1
		current_index = (last_index + 1) % total_active_clients
		c.execute(
			"INSERT OR REPLACE INTO cycle_state (cycle_name, last_account_index) VALUES (?, ?)",
			(cycle_name, current_index)
		)
		conn.commit()
		return active_clients[current_index]
	finally:
		conn.close()


def get_accounts_ordered_for_cycle(cycle_name: str, runtime_clients: list[tuple[object, dict]]):
	active_clients = connected_clients(runtime_clients)
	total_active_clients = len(active_clients)
	if total_active_clients == 0:
		logging.warning(f"get_accounts_ordered_for_cycle ({cycle_name}): no connected clients")
		return []

	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("SELECT last_account_index FROM cycle_state WHERE cycle_name = ?", (cycle_name,))
		row = c.fetchone()
		if not row:
			c.execute("INSERT INTO cycle_state (cycle_name, last_account_index) VALUES (?, ?)", (cycle_name, -1))
			conn.commit()
			last_index = -1
		else:
			last_index = row["last_account_index"]
	finally:
		conn.close()

	start_index = (last_index + 1) % total_active_clients
	return active_clients[start_index:] + active_clients[:start_index]


def record_account_cycle_success(cycle_name: str, account_db_id: int, runtime_clients: list[tuple[object, dict]]) -> bool:
	for index, (_, data) in enumerate(connected_clients(runtime_clients)):
		if data.get("id") == account_db_id:
			conn = get_db_connection()
			c = conn.cursor()
			try:
				c.execute(
					"INSERT OR REPLACE INTO cycle_state (cycle_name, last_account_index) VALUES (?, ?)",
					(cycle_name, index)
				)
				conn.commit()
				return True
			finally:
				conn.close()
	return False
