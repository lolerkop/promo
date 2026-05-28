import html
import io

from aiogram.types import BufferedInputFile, Document, Message

from services.prompt_text import MAX_PROMPT_FILE_BYTES, PromptTextError, decode_prompt_file_bytes

from .bot_instance import bot


def _safe_prompt_filename(filename: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in filename).strip("._")
    if not safe:
        safe = "prompt.txt"
    if not safe.lower().endswith(".txt"):
        safe += ".txt"
    if len(safe) > 120:
        safe = safe[:116] + ".txt"
    return safe


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


async def send_prompt_preview_file(
    message: Message,
    prompt_text: str | None,
    filename: str,
    caption: str,
) -> None:
    prompt = (prompt_text or "").strip()
    if not prompt:
        return

    document = BufferedInputFile(prompt.encode("utf-8"), filename=_safe_prompt_filename(filename))
    await message.answer_document(document=document, caption=html.escape(caption)[:1024])
