import unittest

from services.account_health import format_account_health_result


class AccountHealthFormatTests(unittest.TestCase):
	def test_result_escapes_html_and_expands_status(self):
		text = format_account_health_result({
			"account_id": 7,
			"label": "<main>",
			"status": "warning",
			"details": "Name <bad>",
		})

		self.assertIn("<b>&lt;main&gt;</b>", text)
		self.assertIn("ID: <code>7</code>", text)
		self.assertIn("Status: <b>WARNING</b>", text)
		self.assertIn("Name &lt;bad&gt;", text)

	def test_result_truncates_long_details(self):
		text = format_account_health_result({
			"account_id": 1,
			"label": "acc",
			"status": "bad",
			"details": "x" * 500,
		})

		self.assertIn("Status: <b>BAD</b>", text)
		self.assertIn("...", text)
		self.assertLess(len(text), 430)


if __name__ == "__main__":
	unittest.main()
