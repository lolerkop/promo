import os
import sqlite3
import tempfile
import unittest

import services.trigger_followup as followup


class TriggerFollowupTests(unittest.TestCase):
	def setUp(self):
		self.temp_dir = tempfile.TemporaryDirectory()
		self.db_path = os.path.join(self.temp_dir.name, "test.sqlite")
		conn = sqlite3.connect(self.db_path)
		conn.execute("""
			CREATE TABLE trigger_reply_threads (
				id INTEGER PRIMARY KEY AUTOINCREMENT,
				chat_id INTEGER NOT NULL,
				source_user_id INTEGER NOT NULL,
				source_message_id INTEGER NOT NULL,
				bot_account_id INTEGER,
				bot_user_id INTEGER,
				bot_message_id INTEGER NOT NULL,
				trigger_details TEXT,
				followup_sent INTEGER DEFAULT 0,
				created_at TEXT NOT NULL,
				updated_at TEXT NOT NULL,
				UNIQUE(chat_id, bot_message_id)
			)
		""")
		conn.commit()
		conn.close()

		def get_test_db_connection():
			conn = sqlite3.connect(self.db_path)
			conn.row_factory = sqlite3.Row
			return conn

		self.original_get_db_connection = followup.get_db_connection
		followup.get_db_connection = get_test_db_connection

	def tearDown(self):
		followup.get_db_connection = self.original_get_db_connection
		self.temp_dir.cleanup()

	def test_records_and_marks_trigger_reply_thread(self):
		followup.record_trigger_reply_thread(-1001, 42, 10, 7, 700, 11, "trigger_intent:vpn")
		thread = followup.get_trigger_reply_thread(-1001, 11)

		self.assertIsNotNone(thread)
		self.assertEqual(thread["source_user_id"], 42)
		self.assertEqual(thread["followup_sent"], 0)

		followup.mark_trigger_followup_sent(thread["id"])
		updated = followup.get_trigger_reply_thread(-1001, 11)
		self.assertEqual(updated["followup_sent"], 1)

	def test_quick_relevance_accepts_vpn_context(self):
		self.assertEqual(followup.quick_followup_relevance("а какой впн норм?")[0], True)
		self.assertEqual(followup.quick_followup_relevance("ютуб опять не открывается")[0], True)

	def test_quick_relevance_rejects_provocations_and_empty_replies(self):
		self.assertEqual(followup.quick_followup_relevance("нейросеть сколько будет 2+2?")[0], False)
		self.assertEqual(followup.quick_followup_relevance("ты бот?")[0], False)
		self.assertEqual(followup.quick_followup_relevance("ах")[0], False)


if __name__ == "__main__":
	unittest.main()
