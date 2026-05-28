MAX_PROMPT_FILE_BYTES = 512 * 1024


class PromptTextError(ValueError):
    pass


def normalize_prompt_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff").strip()


def decode_prompt_file_bytes(payload: bytes, filename: str | None = None) -> str:
    if filename and not filename.lower().endswith(".txt"):
        raise PromptTextError("Нужен файл с расширением .txt.")
    if not payload:
        raise PromptTextError("Файл пустой.")
    if len(payload) > MAX_PROMPT_FILE_BYTES:
        max_kb = MAX_PROMPT_FILE_BYTES // 1024
        raise PromptTextError(f"Файл слишком большой. Максимум {max_kb} KB.")

    decoded = None
    last_error = None
    encodings = ("utf-16",) if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "cp1251", "utf-16")
    for encoding in encodings:
        try:
            candidate = payload.decode(encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        if "\x00" in candidate and encoding != "utf-16":
            continue
        decoded = candidate
        break

    if decoded is None:
        raise PromptTextError(f"Не удалось прочитать текст файла: {last_error}")

    prompt = normalize_prompt_text(decoded)
    if not prompt:
        raise PromptTextError("Файл не содержит текста промпта.")
    return prompt
