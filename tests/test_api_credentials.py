import os
import sqlite3
import tempfile
import unittest

import services.api_credentials as api_credentials


class ApiCredentialsTests(unittest.TestCase):
	def setUp(self):
		self.temp_dir = tempfile.TemporaryDirectory()
		self.db_path = os.path.join(self.temp_dir.name, "test.sqlite")
		conn = sqlite3.connect(self.db_path)
		conn.execute("""
			CREATE TABLE telegram_api_credentials (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				api_id INTEGER NOT NULL,
				api_hash TEXT NOT NULL,
				label TEXT,
				max_accounts INTEGER DEFAULT 10,
				is_active INTEGER DEFAULT 1,
				created_at TEXT NOT NULL,
				updated_at TEXT NOT NULL,
				UNIQUE(api_id, api_hash)
			)
		""")
		conn.execute("""
			CREATE TABLE accounts (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				session_name TEXT,
				api_id INTEGER,
				api_hash TEXT
			)
		""")
		conn.commit()
		conn.close()

		def get_test_db_connection():
			conn = sqlite3.connect(self.db_path)
			conn.row_factory = sqlite3.Row
			return conn

		self.original_get_db_connection = api_credentials.get_db_connection
		api_credentials.get_db_connection = get_test_db_connection

	def tearDown(self):
		api_credentials.get_db_connection = self.original_get_db_connection
		self.temp_dir.cleanup()

	def _add_account(self, session_name: str, api_id: int, api_hash: str):
		conn = sqlite3.connect(self.db_path)
		conn.execute(
			"INSERT INTO accounts (session_name, api_id, api_hash) VALUES (?, ?, ?)",
			(session_name, api_id, api_hash)
		)
		conn.commit()
		conn.close()

	def test_available_pair_prefers_lower_account_count(self):
		api_credentials.add_api_credential(111, "hash1", "api-1", max_accounts=10)
		api_credentials.add_api_credential(222, "hash2", "api-2", max_accounts=10)
		self._add_account("acc1", 111, "hash1")

		selected = api_credentials.get_available_api_credential()
		self.assertEqual(selected["api_id"], 222)

	def test_full_and_disabled_pairs_are_skipped(self):
		first_id = api_credentials.add_api_credential(111, "hash1", "api-1", max_accounts=1)
		second_id = api_credentials.add_api_credential(222, "hash2", "api-2", max_accounts=10)
		self._add_account("acc1", 111, "hash1")
		api_credentials.set_api_credential_status(second_id, False)

		self.assertIsNone(api_credentials.get_available_api_credential())
		self.assertTrue(api_credentials.set_api_credential_status(first_id, False))


if __name__ == "__main__":
	unittest.main()
