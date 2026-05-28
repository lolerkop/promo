import io

from aiogram.types import Document

from services.prompt_text import MAX_PROMPT_FILE_BYTES, PromptTextError, decode_prompt_file_bytes

from .bot_instance import bot


async def read_prompt_document(document: Document | None) -> str:
    if document is None:
        raise PromptTextError("Ожидается .txt файл с промптом.")

    filename = document.file_name or ""
    if not filename.lower().endswith(".txt"):
        raise PromptTextError("Ожидается файл .txt.")
    if document.file_size and document.file_size > MAX_PROMPT_FILE_BYTES:
        max_kb = MAX_PROMPT_FILE_BYTES // 1024
        raise PromptTextError(f"Файл слишком большой. Максимум {max_kb} KB.")

    buffer = io.BytesIO()
    await bot.download(document, destination=buffer)
    return decode_prompt_file_bytes(buffer.getvalue(), filename)
