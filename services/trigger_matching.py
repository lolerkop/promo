def chat_id_variants(chat_id) -> tuple[int, ...]:
	variants: list[int] = []
	try:
		base_id = int(chat_id)
	except (TypeError, ValueError):
		return tuple()

	def add(value: int):
		if value not in variants:
			variants.append(value)

	add(base_id)
	abs_text = str(abs(base_id))
	if base_id < 0:
		if abs_text.startswith("100") and len(abs_text) > 3:
			raw_channel_id = int(abs_text[3:])
			add(raw_channel_id)
			add(-raw_channel_id)
		else:
			add(abs(base_id))
	elif base_id > 0:
		add(int(f"-100{base_id}"))
		add(-base_id)

	return tuple(variants)


def sql_placeholders(values: tuple[int, ...]) -> str:
	return ",".join("?" for _ in values)


def linked_chat_peer_from_db(value):
	text_value = str(value or "").strip()
	if not text_value:
		return None
	try:
		linked_chat_id = int(text_value)
	except ValueError:
		return text_value
	if linked_chat_id > 0:
		return int(f"-100{linked_chat_id}")
	return linked_chat_id


def matching_keyword_from_rows(rows, text: str | None) -> str | None:
	text_lower = (text or "").lower()
	if not text_lower:
		return None
	for row in rows:
		keyword = (row["keyword"] or "").lower()
		if keyword and keyword in text_lower:
			return keyword
	return None
