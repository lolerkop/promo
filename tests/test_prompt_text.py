import unittest

from services.prompt_text import (
    MAX_PROMPT_FILE_BYTES,
    PromptTextError,
    decode_prompt_file_bytes,
    normalize_prompt_text,
)


class PromptTextTests(unittest.TestCase):
    def test_normalize_keeps_prompt_lines(self):
        text = normalize_prompt_text("\ufeff  line one\r\nline two\r")
        self.assertEqual(text, "line one\nline two")

    def test_decodes_utf8_sig_txt(self):
        payload = "\ufeffживой промпт\nстрока 2".encode("utf-8-sig")
        self.assertEqual(
            decode_prompt_file_bytes(payload, "prompt.txt"),
            "живой промпт\nстрока 2",
        )

    def test_decodes_cp1251_txt(self):
        payload = "промпт из блокнота".encode("cp1251")
        self.assertEqual(decode_prompt_file_bytes(payload, "prompt.txt"), "промпт из блокнота")

    def test_decodes_utf16_txt(self):
        payload = "длинный промпт".encode("utf-16")
        self.assertEqual(decode_prompt_file_bytes(payload, "prompt.txt"), "длинный промпт")

    def test_rejects_non_txt(self):
        with self.assertRaises(PromptTextError):
            decode_prompt_file_bytes(b"prompt", "prompt.docx")

    def test_rejects_empty_prompt(self):
        with self.assertRaises(PromptTextError):
            decode_prompt_file_bytes(b"   \n", "prompt.txt")

    def test_rejects_too_large_file(self):
        with self.assertRaises(PromptTextError):
            decode_prompt_file_bytes(b"x" * (MAX_PROMPT_FILE_BYTES + 1), "prompt.txt")


if __name__ == "__main__":
    unittest.main()
