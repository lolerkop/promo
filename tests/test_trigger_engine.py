import unittest
from unittest.mock import patch

from services.trigger_engine import (
	TriggerDecision,
	best_indirect_marker_match,
	best_local_trigger_match,
	compact_text,
	evaluate_trigger_for_message,
	normalize_text,
)


class Row(dict):
	def __getitem__(self, key):
		return self.get(key)


class TriggerEngineTests(unittest.TestCase):
	def test_normalize_handles_case_punctuation_and_yo(self):
		self.assertEqual(normalize_text("НУЖЕН!!! Ё-VPN  "), "нужен е vpn")

	def test_compact_handles_spaced_vpn(self):
		self.assertIn("vpn", compact_text("нужен V P N"))
		self.assertIn("впн", compact_text("нужен В П Н"))

	def test_vpn_aliases_match_latin_and_cyrillic(self):
		decision = best_local_trigger_match(
			"подскажите нормальный В П Н",
			global_responses=[Row(keyword="vpn", answer="", response_type="openai")],
		)
		self.assertTrue(decision.matched)
		self.assertEqual(decision.keyword, "vpn")

	def test_category_beats_global(self):
		decision = best_local_trigger_match(
			"нужен vpn срочно",
			category_keywords=[Row(keyword="vpn", answer="cat", response_type="predefined")],
			global_responses=[Row(keyword="нужен vpn", answer="global", response_type="predefined")],
			category_name="vpn",
		)
		self.assertTrue(decision.matched)
		self.assertEqual(decision.source, "category")
		self.assertEqual(decision.answer, "cat")

	def test_longer_phrase_beats_shorter_in_same_scope(self):
		decision = best_local_trigger_match(
			"мне нужен нормальный впн",
			global_responses=[
				Row(keyword="впн", answer="short", response_type="predefined"),
				Row(keyword="нужен нормальный впн", answer="long", response_type="predefined"),
			],
		)
		self.assertTrue(decision.matched)
		self.assertEqual(decision.answer, "long")

	def test_intent_phrase_match(self):
		decision = best_local_trigger_match(
			"ютуб опять не открывается",
			intent_phrase_rows=[
				Row(
					phrase="ютуб не открывается",
					match_type="phrase",
					intent_id=1,
					intent_name="плохо работает связь",
					answer="",
					response_type="openai",
				)
			],
		)
		self.assertTrue(decision.matched)
		self.assertEqual(decision.intent_name, "плохо работает связь")

	def test_indirect_phrase_does_not_reply_directly(self):
		decision = best_local_trigger_match(
			"ютуб опять странно работает",
			intent_phrase_rows=[
				Row(
					phrase="ютуб",
					match_type="indirect",
					intent_id=1,
					intent_name="плохо работает связь",
					answer="",
					response_type="openai",
				)
			],
		)
		self.assertFalse(decision.matched)
		self.assertEqual(decision.reason, "no_local_match")

	def test_indirect_marker_match_is_detected(self):
		decision = best_indirect_marker_match(
			"ютуб опять странно работает",
			intent_phrase_rows=[
				Row(
					phrase="ютуб",
					match_type="indirect",
					intent_id=1,
					intent_name="плохо работает связь",
					answer="",
					response_type="openai",
				)
			],
		)
		self.assertFalse(decision.matched)
		self.assertEqual(decision.indirect_keyword, "ютуб")
		self.assertEqual(decision.reason, "indirect_marker_match")

	def test_semantic_is_not_called_without_indirect_marker(self):
		with patch("services.trigger_engine.fetch_category_keywords", return_value=[]), \
			 patch("services.trigger_engine.fetch_global_responses", return_value=[]), \
			 patch("services.trigger_engine.fetch_intent_phrase_rows", return_value=[]), \
			 patch("services.trigger_engine.classify_semantic_intent") as classifier:
			decision = evaluate_trigger_for_message(
				"обычное сообщение без маркеров",
				allow_global=True,
				allow_semantic=True,
				respect_cooldown=False,
			)
		classifier.assert_not_called()
		self.assertFalse(decision.matched)
		self.assertEqual(decision.reason, "no_indirect_marker")

	def test_semantic_is_called_after_indirect_marker(self):
		semantic_result = TriggerDecision(
			matched=True,
			source="semantic",
			match_type="semantic",
			intent_name="плохо работает связь",
			response_type="openai",
			confidence=0.9,
			reason="semantic_match",
		)
		with patch("services.trigger_engine.fetch_category_keywords", return_value=[]), \
			 patch("services.trigger_engine.fetch_global_responses", return_value=[]), \
			 patch("services.trigger_engine.fetch_intent_phrase_rows", return_value=[
				 Row(
					 phrase="ютуб",
					 match_type="indirect",
					 intent_id=1,
					 intent_name="плохо работает связь",
					 answer="",
					 response_type="openai",
				 )
			 ]), \
			 patch("services.trigger_engine.classify_semantic_intent", return_value=semantic_result) as classifier:
			decision = evaluate_trigger_for_message(
				"ютуб опять странно работает",
				allow_global=True,
				allow_semantic=True,
				respect_cooldown=False,
			)
		classifier.assert_called_once()
		self.assertTrue(decision.matched)
		self.assertEqual(decision.indirect_keyword, "ютуб")


if __name__ == "__main__":
	unittest.main()
