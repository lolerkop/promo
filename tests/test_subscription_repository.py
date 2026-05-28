import os
import sqlite3
import tempfile
import unittest

import services.subscription_repository as subscriptions


class SubscriptionRepositoryTests(unittest.TestCase):
	def setUp(self):
		self.temp_dir = tempfile.TemporaryDirectory()
		self.db_path = os.path.join(self.temp_dir.name, "test.sqlite")
		conn = sqlite3.connect(self.db_path)
		conn.execute("""
			CREATE TABLE account_entity_memberships (
				account_id INTEGER NOT NULL,
				entity_type TEXT NOT NULL,
				entity_id INTEGER NOT NULL,
				identifier TEXT,
				status TEXT DEFAULT 'active',
				joined_at TEXT NOT NULL,
				updated_at TEXT NOT NULL,
				PRIMARY KEY (account_id, entity_type, entity_id)
			)
		""")
		conn.commit()
		conn.close()

		def get_test_db_connection():
			conn = sqlite3.connect(self.db_path)
			conn.row_factory = sqlite3.Row
			return conn

		self.original_get_db_connection = subscriptions.get_db_connection
		subscriptions.get_db_connection = get_test_db_connection

	def tearDown(self):
		subscriptions.get_db_connection = self.original_get_db_connection
		self.temp_dir.cleanup()

	def test_records_every_successful_account_membership(self):
		subscriptions.record_entity_memberships([1, 2, 2, None], "channel", -1001, "https://t.me/test")
		self.assertEqual(subscriptions.get_account_subscription_loads(), {1: 1, 2: 1})

	def test_rejoin_reactivates_left_membership_without_double_counting(self):
		subscriptions.record_entity_memberships([1], "group", -2001, "@chat")
		subscriptions.mark_entity_memberships_left("group", [-2001], [1])
		self.assertEqual(subscriptions.get_account_subscription_loads(), {})

		subscriptions.record_entity_memberships([1], "group", -2001, "@chat")
		self.assertEqual(subscriptions.get_account_subscription_loads(), {1: 1})


if __name__ == "__main__":
	unittest.main()
