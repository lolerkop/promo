import os
import sqlite3
import tempfile
import unittest

import db
from services import proxy_repository


class ProxyRepositoryTests(unittest.TestCase):
	def setUp(self):
		self.temp_dir = tempfile.TemporaryDirectory()
		self.db_path = os.path.join(self.temp_dir.name, "test.sqlite")
		self.original_get_current_db_path = db.get_current_db_path
		db.get_current_db_path = lambda: self.db_path
		db.create_tables()

	def tearDown(self):
		db.get_current_db_path = self.original_get_current_db_path
		self.temp_dir.cleanup()

	def test_add_bulk_marks_past_expiry_as_expired(self):
		added, skipped = proxy_repository.add_proxies_bulk([
			{
				"proxy_type": "socks5",
				"proxy_ip": "127.0.0.1",
				"proxy_port": 1080,
				"expires_at": "2000-01-01",
			}
		])

		self.assertEqual((added, skipped), (1, 0))
		details = proxy_repository.get_proxy_details(1)
		self.assertEqual(details["computed_status"], "expired")

	def test_delete_proxy_clears_assigned_account_proxy_fields(self):
		with db.db_transaction() as (_, cursor):
			cursor.execute(
				"""
				INSERT INTO accounts (
					session_name, phone, api_id, api_hash, label, user_id,
					proxy_type, proxy_ip, proxy_port, proxy_username, proxy_password
				)
				VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
				""",
				("acc", "+1", 1, "hash", "acc", 10, "socks5", "127.0.0.1", 1080, "u", "p")
			)
			account_id = cursor.lastrowid
			cursor.execute(
				"""
				INSERT INTO proxies (
					proxy_type, proxy_ip, proxy_port, proxy_username, proxy_password, assigned_account_id, status
				)
				VALUES (?, ?, ?, ?, ?, ?, ?)
				""",
				("socks5", "127.0.0.1", 1080, "u", "p", account_id, "active")
			)
			proxy_id = cursor.lastrowid

		self.assertTrue(proxy_repository.delete_proxy(proxy_id))
		conn = sqlite3.connect(self.db_path)
		conn.row_factory = sqlite3.Row
		try:
			account = conn.execute("SELECT proxy_ip, proxy_port FROM accounts WHERE id = ?", (account_id,)).fetchone()
			self.assertIsNone(account["proxy_ip"])
			self.assertIsNone(account["proxy_port"])
		finally:
			conn.close()


if __name__ == "__main__":
	unittest.main()
