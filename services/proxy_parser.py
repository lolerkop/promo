import re


def normalize_proxy_type(value: str | None) -> str | None:
	value = (value or "").strip().lower()
	if value == "socks":
		return "socks5"
	if value in {"socks5", "socks4", "http"}:
		return value
	return None


def looks_like_proxy_expiry(value: str | None) -> bool:
	value = (value or "").strip()
	if not value:
		return False
	return bool(re.match(
		r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?$",
		value
	))


def split_proxy_password_and_expiry(password_parts: list[str]) -> tuple[str | None, str | None]:
	max_expiry_parts = min(4, max(0, len(password_parts) - 1))
	for expiry_parts_count in range(max_expiry_parts, 0, -1):
		expires_at = ":".join(password_parts[-expiry_parts_count:]).strip()
		if looks_like_proxy_expiry(expires_at):
			password = ":".join(password_parts[:-expiry_parts_count]) or None
			return password, expires_at
	return ":".join(password_parts) or None, None


def build_proxy_import_data(proxy_type: str | None, ip: str, port: str, tail: list[str]) -> dict | None:
	try:
		proxy_port = int(port)
	except (TypeError, ValueError):
		return None

	proxy_type = normalize_proxy_type(proxy_type) or "socks5"
	proxy_username = None
	proxy_password = None
	expires_at = None

	if tail:
		if len(tail) < 2:
			return None

		inline_type = normalize_proxy_type(tail[0])
		if inline_type and len(tail) >= 3:
			proxy_type = inline_type
			proxy_username = tail[1] or None
			password_parts = tail[2:]
		else:
			proxy_username = tail[0] or None
			password_parts = tail[1:]

		proxy_password, expires_at = split_proxy_password_and_expiry(password_parts)

	return {
		"proxy_type": proxy_type,
		"proxy_ip": ip,
		"proxy_port": proxy_port,
		"proxy_username": proxy_username,
		"proxy_password": proxy_password,
		"expires_at": expires_at,
		"status": "active",
	}


def parse_proxy_import_line(line: str) -> dict | None:
	line = (line or "").strip()
	if not line or line.startswith("#"):
		return None

	bracket_match = re.match(
		r"^(?:(socks5|socks4|http|socks):)?\[([^\]]+)\]:(\d+)(?::(.+))?$",
		line,
		flags=re.IGNORECASE
	)
	if bracket_match:
		proxy_type, ip, port, tail_raw = bracket_match.groups()
		return build_proxy_import_data(proxy_type, ip, port, tail_raw.split(":") if tail_raw else [])

	parts = line.split(":")
	proxy_type = normalize_proxy_type(parts[0]) if parts else None
	if proxy_type:
		parts.pop(0)

	if len(parts) < 2:
		return None
	return build_proxy_import_data(proxy_type, parts[0], parts[1], parts[2:])
