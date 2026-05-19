import os
import sqlite3
import tempfile
import unittest

import services.rotation as rotation


class FakeClient:
	def __init__(self, connected=True):
		self.connected = connected

	def is_connected(self):
		return self.connected


class RotationTests(unittest.TestCase):
	def setUp(self):
		self.temp_dir = tempfile.TemporaryDirectory()
		self.db_path = os.path.join(self.temp_dir.name, "test.sqlite")
		conn = sqlite3.connect(self.db_path)
		conn.execute("CREATE TABLE cycle_state (cycle_name TEXT PRIMARY KEY, last_account_index INTEGER)")
		conn.commit()
		conn.close()

		def get_test_db_connection():
			conn = sqlite3.connect(self.db_path)
			conn.row_factory = sqlite3.Row
			return conn

		self.original_get_db_connection = rotation.get_db_connection
		rotation.get_db_connection = get_test_db_connection

	def tearDown(self):
		rotation.get_db_connection = self.original_get_db_connection
		self.temp_dir.cleanup()

	def test_ordered_cycle_starts_after_last_success(self):
		clients = [
			(FakeClient(), {"id": 1}),
			(FakeClient(), {"id": 2}),
			(FakeClient(), {"id": 3}),
		]
		self.assertEqual([data["id"] for _, data in rotation.get_accounts_ordered_for_cycle("chat", clients)], [1, 2, 3])
		self.assertTrue(rotation.record_account_cycle_success("chat", 2, clients))
		self.assertEqual([data["id"] for _, data in rotation.get_accounts_ordered_for_cycle("chat", clients)], [3, 1, 2])

	def test_disconnected_clients_are_skipped(self):
		clients = [
			(FakeClient(), {"id": 1}),
			(FakeClient(False), {"id": 2}),
			(FakeClient(), {"id": 3}),
		]
		self.assertEqual([data["id"] for _, data in rotation.get_accounts_ordered_for_cycle("chat", clients)], [1, 3])

	def test_next_account_updates_cycle_state(self):
		clients = [
			(FakeClient(), {"id": 1}),
			(FakeClient(), {"id": 2}),
		]
		_, first = rotation.get_next_account_in_cycle("chat", clients)
		_, second = rotation.get_next_account_in_cycle("chat", clients)
		self.assertEqual(first["id"], 1)
		self.assertEqual(second["id"], 2)


if __name__ == "__main__":
	unittest.main()
