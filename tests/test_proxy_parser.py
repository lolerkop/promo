import unittest

from services.proxy_parser import parse_proxy_import_line


class ProxyParserTests(unittest.TestCase):
	def test_plain_proxy_with_auth(self):
		parsed = parse_proxy_import_line("185.228.194.57:51524:user:pass")
		self.assertEqual(parsed["proxy_type"], "socks5")
		self.assertEqual(parsed["proxy_ip"], "185.228.194.57")
		self.assertEqual(parsed["proxy_port"], 51524)
		self.assertEqual(parsed["proxy_username"], "user")
		self.assertEqual(parsed["proxy_password"], "pass")
		self.assertIsNone(parsed["expires_at"])

	def test_inline_type_after_port(self):
		parsed = parse_proxy_import_line("185.228.194.57:51524:SOCKS:user:pass")
		self.assertEqual(parsed["proxy_type"], "socks5")
		self.assertEqual(parsed["proxy_username"], "user")
		self.assertEqual(parsed["proxy_password"], "pass")
		self.assertIsNone(parsed["expires_at"])

	def test_expiry_is_only_parsed_when_date_like(self):
		parsed = parse_proxy_import_line("socks5:185.228.194.57:51524:user:pass:2026-06-01")
		self.assertEqual(parsed["proxy_password"], "pass")
		self.assertEqual(parsed["expires_at"], "2026-06-01")

	def test_ipv6_bracket_proxy(self):
		parsed = parse_proxy_import_line("http:[2001:db8::1]:8080:user:pa:ss:2026-06-01 12:30:00")
		self.assertEqual(parsed["proxy_type"], "http")
		self.assertEqual(parsed["proxy_ip"], "2001:db8::1")
		self.assertEqual(parsed["proxy_password"], "pa:ss")
		self.assertEqual(parsed["expires_at"], "2026-06-01 12:30:00")

	def test_invalid_line(self):
		self.assertIsNone(parse_proxy_import_line("not-enough"))


if __name__ == "__main__":
	unittest.main()
