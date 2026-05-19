import unittest

from services.trigger_matching import (chat_id_variants, linked_chat_peer_from_db,
                                       matching_keyword_from_rows,
                                       sql_placeholders)


class Row(dict):
	def __getitem__(self, key):
		return self.get(key)


class TriggerMatchingTests(unittest.TestCase):
	def test_chat_id_variants_for_supergroup_peer(self):
		self.assertEqual(chat_id_variants(-1003904631544), (-1003904631544, 3904631544, -3904631544))

	def test_chat_id_variants_for_raw_channel_id(self):
		self.assertEqual(chat_id_variants(3904631544), (3904631544, -1003904631544, -3904631544))

	def test_linked_chat_peer_from_db(self):
		self.assertEqual(linked_chat_peer_from_db("3904631544"), -1003904631544)
		self.assertEqual(linked_chat_peer_from_db("-1003904631544"), -1003904631544)
		self.assertEqual(linked_chat_peer_from_db("@discussion"), "@discussion")

	def test_matching_keyword_from_rows(self):
		rows = [Row(keyword="арбуз"), Row(keyword="дыня")]
		self.assertEqual(matching_keyword_from_rows(rows, "хочу АрБуЗ"), "арбуз")
		self.assertIsNone(matching_keyword_from_rows(rows, "яблоко"))

	def test_sql_placeholders(self):
		self.assertEqual(sql_placeholders((1, 2, 3)), "?,?,?")


if __name__ == "__main__":
	unittest.main()
