import logging
import random
import re

from g4f.client import Client as G4FClient
from openai import (APIConnectionError, APIStatusError, AuthenticationError,
                    BadRequestError, OpenAI, PermissionDeniedError,
                    RateLimitError)

from db import (get_active_openai_api_keys_for_cycle, get_templates,
                record_openai_key_failure, record_openai_key_usage)

g4f_client = G4FClient()
TEMPLATE_PATTERN = re.compile(r'(\d+\[[^\]\n]+\])')


def _generate_with_g4f(messages, g4f_model, temperature, max_tokens):
		data = {
				"content": "Извините, G4F не смог сформировать корректный ответ.",
				"service_used": "g4f",
				"success": False,
		}
		try:
				resp = g4f_client.chat.completions.create(
						model=g4f_model,
						messages=messages,
						temperature=temperature,
						max_tokens=max_tokens,
				)
				if resp.choices and resp.choices[0].message and resp.choices[0].message.content:
						data.update(
								{"content": resp.choices[0].message.content, "success": True}
						)
				else:
						logging.error("G4F вернул пустую структуру.")
		except Exception as e:
				logging.error("Сбой G4F: %s – %s", type(e).__name__, e)
				data["content"] = "Извините, не удалось сгенерировать ответ через G4F."
		return data


def generate_ai_response(
				messages,
				openai_model="gpt-3.5-turbo",
				g4f_model="gpt-4o-mini",
				provider="openai_g4f",
				temperature=0.7,
				max_tokens=150,
):
		"""Основная точка входа."""
		result = {
				"content": "Ошибка AI: не удалось обработать запрос.",
				"service_used": "none",
				"success": False,
		}

		if provider != "openai_g4f":
				logging.warning("Unsupported AI provider '%s'. Falling back to OpenAI/G4F.", provider)

		keys = get_active_openai_api_keys_for_cycle()
		if not keys:
				logging.warning(
						"Активные OpenAI-ключи не найдены. Переходим на G4F."
				)
				return _generate_with_g4f(messages, g4f_model, temperature, max_tokens)

		for key in keys:
				key_id, api_key = key["id"], key["api_key"]
				logging.info("Пробуем ключ OpenAI id=%s", key_id)
				client = OpenAI(api_key=api_key)

				try:
						resp = client.chat.completions.create(
								model=openai_model,
								messages=messages,
								temperature=temperature,
								max_tokens=max_tokens,
						)
						if resp.choices and resp.choices[0].message and resp.choices[0].message.content:
								result.update(
										{
												"content": resp.choices[0].message.content,
												"service_used": "openai",
												"success": True,
										}
								)
								record_openai_key_usage(key_id)
								return result
						logging.error("OpenAI вернул пустой ответ.")
				except (AuthenticationError, PermissionDeniedError) as e:
						logging.warning(
								"Auth/Permission (%s). Деактивируем ключ %s.",
								type(e).__name__,
								key_id,
						)
						record_openai_key_failure(key_id, temporarily_deactivate=True)
				except RateLimitError as e:
						logging.warning("Rate limit (%s) на ключе %s.", type(e).__name__, key_id)
						record_openai_key_failure(key_id)
				except (BadRequestError, APIStatusError, APIConnectionError) as e:
						logging.warning(
								"API-ошибка (%s) на ключе %s.", type(e).__name__, key_id
						)
						record_openai_key_failure(key_id)
				except Exception as e:
						logging.error(
								"Непредвиденный сбой (%s) на ключе %s.", type(e).__name__, key_id
						)
						record_openai_key_failure(key_id)

		logging.warning("Все ключи OpenAI исчерпаны. Переходим на G4F.")
		return _generate_with_g4f(messages, g4f_model, temperature, max_tokens)

def template_response() -> str | None:
	template = get_templates()

	if not template:
		logging.warning("Non-AI mode is enabled, but the template list is empty")
		return

	template = TEMPLATE_PATTERN.findall(template)
	template.sort(key=lambda data: int(data[0]), reverse=True)
	rand_int = random.randint(1, len(template)) - 1

	if rand_int > len(template):
		logging.error("Random number exceeds limit")
		return
	
	pattern = re.compile(r'\[([^\]]+)\]')

	text = pattern.findall(template[rand_int])

	if not text:
		logging.error("Unable to retrieve data from []")
		return

	return text[0]
