import os
import sqlite3
import tempfile
import unittest

import db


class AccountProxyTransactionTests(unittest.TestCase):
	def setUp(self):
		self.temp_dir = tempfile.TemporaryDirectory()
		self.db_path = os.path.join(self.temp_dir.name, "test.sqlite")
		self.original_get_current_db_path = db.get_current_db_path
		db.get_current_db_path = lambda: self.db_path
		db.create_tables()

	def tearDown(self):
		db.get_current_db_path = self.original_get_current_db_path
		self.temp_dir.cleanup()

	def _account_data(self, session_name: str):
		return {
			"session_name": session_name,
			"phone": "+10000000000",
			"api_id": 123,
			"api_hash": "hash",
			"label": session_name,
			"user_id": 111,
			"proxy_type": "socks5",
			"proxy_ip": "127.0.0.1",
			"proxy_port": 1080,
			"proxy_username": "user",
			"proxy_password": "pass",
		}

	def test_create_account_assigns_proxy_in_one_transaction(self):
		with db.db_transaction() as (_, cursor):
			cursor.execute(
				"INSERT INTO proxies (proxy_type, proxy_ip, proxy_port, status) VALUES (?, ?, ?, ?)",
				("socks5", "127.0.0.1", 1080, "active")
			)
			proxy_id = cursor.lastrowid

		account_id = db.create_account_with_proxy(self._account_data("acc1"), proxy_id=proxy_id)
		conn = sqlite3.connect(self.db_path)
		conn.row_factory = sqlite3.Row
		try:
			row = conn.execute("SELECT assigned_account_id FROM proxies WHERE id = ?", (proxy_id,)).fetchone()
			self.assertEqual(row["assigned_account_id"], account_id)
		finally:
			conn.close()

	def test_proxy_assignment_failure_rolls_back_account_insert(self):
		with db.db_transaction() as (_, cursor):
			cursor.execute(
				"INSERT INTO proxies (proxy_type, proxy_ip, proxy_port, status) VALUES (?, ?, ?, ?)",
				("socks5", "127.0.0.1", 1080, "active")
			)
			proxy_id = cursor.lastrowid

		db.create_account_with_proxy(self._account_data("acc1"), proxy_id=proxy_id)
		with self.assertRaises(sqlite3.IntegrityError):
			db.create_account_with_proxy(self._account_data("acc2"), proxy_id=proxy_id)

		conn = sqlite3.connect(self.db_path)
		try:
			count = conn.execute("SELECT COUNT(*) FROM accounts WHERE session_name = 'acc2'").fetchone()[0]
			self.assertEqual(count, 0)
		finally:
			conn.close()


if __name__ == "__main__":
	unittest.main()
