# C:\Users\27030\Desktop\TEMP\userbot.py
import asyncio
import html
import json
import logging
import os
import random
import time
from collections import defaultdict
from datetime import datetime, timezone

import openai
from telethon import TelegramClient, events
from telethon.errors.rpcbaseerrors import RPCError
from telethon.errors.rpcerrorlist import (ChatAdminRequiredError,
                                          FloodWaitError, PeerIdInvalidError,
                                          PhoneCodeExpiredError,
                                          PhoneCodeInvalidError,
                                          SessionPasswordNeededError,
                                          UserIsBlockedError)
from telethon.sessions import SQLiteSession
from telethon.tl.functions.channels import (JoinChannelRequest,
                                            LeaveChannelRequest)
from telethon.tl.functions.messages import (GetDiscussionMessageRequest,
                                            ImportChatInviteRequest)
from telethon.utils import get_peer_id

from ai_handler import generate_ai_response, template_response
from db import (add_analytics_log, get_account_details,
                get_active_category_prompt_for_chat, get_category_keywords,
                get_config_value, get_current_workspace_id, get_db_connection,
                get_workspace_session_dir,
                list_workspaces, record_account_runtime_event, reset_workspace_context,
                set_config_value, set_workspace_context, update_all_tables)
from services.proxy_repository import (get_account_proxy_block_reason,
                                       refresh_expired_proxies)
from services.rotation import (get_accounts_ordered_for_cycle as rotation_get_accounts_ordered_for_cycle,
                               get_next_account_in_cycle as rotation_get_next_account_in_cycle,
                               record_account_cycle_success as rotation_record_account_cycle_success)
from services.trigger_matching import (chat_id_variants as trigger_chat_id_variants,
                                       linked_chat_peer_from_db,
                                       matching_keyword_from_rows,
                                       sql_placeholders)

try:
		from admin_bot.reporting_utils import send_report
		from admin_bot.reporting_utils import append_auto_comment_report
except ImportError:
		async def send_report(*args, **kwargs):
				logging.error(
						"Failed to import send_report from admin_bot.reporting_utils in userbot.py. Reporting will be disabled.")
		def append_auto_comment_report(*args, **kwargs):
				logging.error(
						"Failed to import append_auto_comment_report from admin_bot.reporting_utils in userbot.py.")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

clients: list[tuple[TelegramClient, dict]] = []
ALL_CLIENT_USER_IDS = set()
workspace_clients: dict[str, list[tuple[TelegramClient, dict]]] = {}
workspace_client_tasks: dict[str, dict[int, asyncio.Task]] = {}
workspace_user_ids: dict[str, set[int]] = {}
cleanup_post_locks_task: asyncio.Task | None = None
REPLY_DELAY_MIN = 20
REPLY_DELAY_MAX = 30
LISTEN_ALL = True
START_TIME = datetime.now(timezone.utc)

conversation_tracker = defaultdict(lambda: defaultdict(int))
client_tasks: dict[int, asyncio.Task] = {}

CLEANUP_INTERVAL_SECONDS = 300
POST_LOCK_TIMEOUT_SECONDS = 900

currently_processing_posts: dict[tuple, float] = {}
processing_posts_lock = asyncio.Lock()


def _sync_all_client_user_ids():
		ALL_CLIENT_USER_IDS.clear()
		for user_ids in workspace_user_ids.values():
				ALL_CLIENT_USER_IDS.update(user_ids)
		for runtime_clients in workspace_clients.values():
				for _, client_data in runtime_clients:
						user_id = client_data.get("user_id") if client_data else None
						if user_id:
								ALL_CLIENT_USER_IDS.add(user_id)


def _sync_legacy_runtime_refs():
		global clients, client_tasks
		workspace_id = get_current_workspace_id()
		clients = workspace_clients.setdefault(workspace_id, [])
		client_tasks = workspace_client_tasks.setdefault(workspace_id, {})
		_sync_all_client_user_ids()


def _chat_id_variants(chat_id) -> tuple[int, ...]:
		return trigger_chat_id_variants(chat_id)


def _sql_placeholders(values: tuple[int, ...]) -> str:
		return sql_placeholders(values)


def _linked_chat_peer_from_db(value):
		return linked_chat_peer_from_db(value)


def _matching_global_keyword_from_rows(rows, text: str | None) -> str | None:
		return matching_keyword_from_rows(rows, text)


def _matching_global_keyword_from_db(text: str | None) -> str | None:
		conn = get_db_connection()
		cursor = conn.cursor()
		try:
				cursor.execute("SELECT keyword FROM responses")
				return _matching_global_keyword_from_rows(cursor.fetchall(), text)
		finally:
				conn.close()


def _get_group_assigned_account_id(chat_id: int) -> int | None:
		chat_id_variants = _chat_id_variants(chat_id)
		if not chat_id_variants:
				return None
		conn = get_db_connection()
		cursor = conn.cursor()
		try:
				cursor.execute(
						f"SELECT assigned_account_id FROM groups WHERE id IN ({_sql_placeholders(chat_id_variants)}) "
						"AND assigned_account_id IS NOT NULL LIMIT 1",
						chat_id_variants
				)
				row = cursor.fetchone()
				return row["assigned_account_id"] if row else None
		finally:
				conn.close()


async def _try_claim_processing_key(key: tuple, timeout_seconds: int, log_prefix: str,
																		account_label: str, account_db_id: int | None = None) -> bool:
		async with processing_posts_lock:
				if key in currently_processing_posts:
						lock_time = currently_processing_posts[key]
						if (time.monotonic() - lock_time) < timeout_seconds:
								logging.info(
										f"{log_prefix}: {key} уже обрабатывается. Аккаунт {account_label} (ID: {account_db_id}) пропускает.")
								return False
						logging.warning(
								f"{log_prefix}: {key} имеет старую блокировку от {lock_time}. Аккаунт {account_label} (ID: {account_db_id}) перехватывает.")

				currently_processing_posts[key] = time.monotonic()
				logging.info(
						f"{log_prefix}: аккаунт {account_label} (ID: {account_db_id}) захватил обработку {key}.")
				return True


async def _release_processing_key(key: tuple[int, int], log_prefix: str, account_label: str,
																	account_db_id: int | None = None):
		async with processing_posts_lock:
				if key in currently_processing_posts:
						del currently_processing_posts[key]
						logging.info(
								f"{log_prefix}: аккаунт {account_label} (ID: {account_db_id}) освободил обработку {key}.")


def get_active_clients(workspace_id: str = None):
		workspace_id = workspace_id or get_current_workspace_id()
		return workspace_clients.setdefault(workspace_id, [])


async def cleanup_stale_post_locks():
		global currently_processing_posts, processing_posts_lock
		while True:
				await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
				async with processing_posts_lock:
						now = time.monotonic()
						stale_keys = [
								key for key, lock_time in currently_processing_posts.items()
								if (now - lock_time) > POST_LOCK_TIMEOUT_SECONDS
						]
						if stale_keys:
								logging.info(f"CLEANUP_POST_LOCKS: Removing {len(stale_keys)} stale post locks.")
								for key in stale_keys:
										if key in currently_processing_posts:
												del currently_processing_posts[key]
						else:
								logging.debug("CLEANUP_POST_LOCKS: No stale post locks to remove.")


def init_listen_all(value=None):
		global LISTEN_ALL
		if value is not None:
				LISTEN_ALL = bool(value)
		else:
				val = get_config_value('listen_all', 'True')
				LISTEN_ALL = (val.lower() == 'true')
		logging.info(f"init_listen_all: LISTEN_ALL = {LISTEN_ALL}")


def get_next_account_in_cycle(cycle_name):
		return rotation_get_next_account_in_cycle(cycle_name, get_active_clients())


def get_accounts_ordered_for_cycle(cycle_name):
		return rotation_get_accounts_ordered_for_cycle(cycle_name, get_active_clients())


def record_account_cycle_success(cycle_name, account_db_id):
		return rotation_record_account_cycle_success(cycle_name, account_db_id, get_active_clients())


def humanize_ai_text(text: str) -> str:
		if not text:
				return ""

		phrases_to_remove = [
				"конечно, вот ваш ответ:", "конечно, вот ответ:", "вот ваш ответ:", "вот ответ:",
				"мой ответ таков:", "мой ответ:", "вот возможный вариант:", "безусловно, вот вариант:",
				"вот один из вариантов:", "вот что я могу предложить:", "вот мой вариант:", "конечно!",
				"разумеется,", "безусловно,", "вот, пожалуйста:",
				"here's a response:", "certainly, here's a response:", "my response is:", "of course!",
				"here is a possible version:", "as an ai language model,", "as an ai,", "as an artificial intelligence,",
				"как ии,", "как языковая модель ии,", "как искусственный интеллект,"
		]
		text_lower_processed = text.lower()
		original_text = text

		for phrase in phrases_to_remove:
				if text_lower_processed.startswith(phrase):
						original_text = original_text[len(phrase):].lstrip(" .,:;-")
						text_lower_processed = original_text.lower()

		if (original_text.startswith('"') and original_text.endswith('"')) or \
						(original_text.startswith("'") and original_text.endswith("'")) or \
						(original_text.startswith('«') and original_text.endswith('»')) or \
						(original_text.startswith('„') and original_text.endswith('“')):
				original_text = original_text[1:-1]

		original_text = original_text.replace("...", ".").replace("!..", "!").replace("?.", "?")
		original_text = original_text.strip()
		return original_text


def generate_vpn_comment(context: str = None, current_account_id_for_log: int = None, chat_id_for_prompt: int = None, dialogue_history: list = None) -> str:
		use_ai = get_config_value("use_ai", "True").lower() == 'true'
		if not use_ai:
			logging.info(f"generate_vpn_comment: Ai отключен, используется шаблон.")
			template = template_response()
			if not template:
				return ""
			return template

		category_prompt = None
		base_prompt_key = "ai_base_prompt"
		max_tokens_key = "ai_max_tokens"
		default_base_prompt_val = "Ты — копирайтер, который пишет уникальные, интересные и вовлекающие комментарии на разные темы."
		default_max_tokens_val = "150"

		persona_system_message_content = "Ты общаешься как обычный, дружелюбный человек в чате. Твои ответы должны быть неформальными и естественными. Избегай любых фраз, которые могут выдать в тебе ИИ или бота. Не используй официальные приветствия или прощания. Будь краток и по делу, как будто ты просто делишься мнением или отвечаешь на вопрос."

		if dialogue_history:
				base_prompt_key = "ai_base_prompt_conversation"
				max_tokens_key = "ai_max_tokens_conversation"
				default_base_prompt_val = get_config_value(base_prompt_key,
																									 "Ты — хороший собеседник, который хочет поддержать тему. Продолжай диалог.")
				default_max_tokens_val = get_config_value(max_tokens_key, "100")

		if chat_id_for_prompt:
				category_prompt_from_db = get_active_category_prompt_for_chat(chat_id_for_prompt)
				if category_prompt_from_db:
						category_prompt = category_prompt_from_db

		base_prompt_to_use = category_prompt if category_prompt else get_config_value(base_prompt_key,
																																									default_base_prompt_val)

		openai_model_db = get_config_value("openai_model", "gpt-3.5-turbo")
		ai_provider_db = get_config_value("ai_provider", "openai")
		try:
				temperature_db = float(get_config_value("ai_temperature", "0.7"))
		except ValueError:
				temperature_db = 0.7
		try:
				max_tokens_db = int(get_config_value(max_tokens_key, default_max_tokens_val))
		except ValueError:
				max_tokens_db = int(default_max_tokens_val)

		messages_for_api = []
		if dialogue_history:
				messages_for_api.append({"role": "system", "content": persona_system_message_content})
				messages_for_api.append({"role": "system", "content": base_prompt_to_use})
				messages_for_api.extend(dialogue_history)
		else:
				messages_for_api.append({"role": "system", "content": persona_system_message_content})
				final_user_prompt = base_prompt_to_use
				if context:
						final_user_prompt += f"\n\nУчитывая эту главную инструкцию, напиши релевантный комментарий к следующему посту (или теме):\n---\n{context}\n---"
				messages_for_api.append({"role": "user", "content": final_user_prompt})

		try:
				messages_str_for_log = json.dumps(messages_for_api, indent=2, ensure_ascii=False)
				logging.info(f"GENERATE_VPN_COMMENT: Полный промпт, отправляемый в AI:\n{messages_str_for_log}")
		except Exception as e:
				logging.error(f"GENERATE_VPN_COMMENT: Не удалось сериализовать промпт для лога: {e}")
				logging.info(f"GENERATE_VPN_COMMENT: Сырой промпт: {messages_for_api}")

		ai_result_data = generate_ai_response(
				messages=messages_for_api,
				openai_model=openai_model_db,
				provider=ai_provider_db,
				temperature=temperature_db,
				max_tokens=max_tokens_db
		)

		comment = ai_result_data["content"]
		comment = humanize_ai_text(comment)

		log_details = f"Service: {ai_result_data['service_used']}, Dialogue: {'Yes' if dialogue_history else 'No'}, CategoryPrompt: {'Yes' if category_prompt else 'No'}"
		if not ai_result_data["success"]:
				log_details += f", Error: {comment[:100]}"

		add_analytics_log(
				account_id=current_account_id_for_log,
				action_type="ai_generated_comment" if not dialogue_history else "ai_dialogue_reply",
				details=log_details,
				success=ai_result_data["success"]
		)

		if not ai_result_data["success"]:
				logging.error(
						f"generate_vpn_comment (Dialogue: {bool(dialogue_history)}): Не удалось получить ответ от AI. Сервис: {ai_result_data['service_used']}. Результат: {comment}")
		else:
				logging.info(
						f"generate_vpn_comment (Dialogue: {bool(dialogue_history)}): Получен ответ от AI ({ai_result_data['service_used']}) (Длина: {len(comment)})")

		return comment


async def anti_spam_send_reply(event, text, user_id, current_account_id_for_log: int = None,
															 action_details: str = "predefined_reply", client_label: str = "N/A",
															 is_dialogue_reply: bool = False):
		min_delay, max_delay = (30, 300) if is_dialogue_reply else (REPLY_DELAY_MIN, REPLY_DELAY_MAX)

		if action_details == "dm_auto_reply":
				min_delay, max_delay = 5, 15

		delay = random.randint(min_delay, max_delay)

		logging.info(
				f"Ждём {delay}s, затем отправляем пользователю {user_id} (Диалог: {is_dialogue_reply}, Детали: {action_details})")
		await asyncio.sleep(delay)

		report_title = "Ответ в диалоге" if is_dialogue_reply else (
				"Автоответ на ЛС" if action_details == "dm_auto_reply" else "Ответ на сообщение")
		status_str = ""
		error_text_report = ""
		sent_successfully = False
		account_info_str = f"<code>{client_label}</code> (ID: {current_account_id_for_log if current_account_id_for_log else 'N/A'})"

		event_text_for_report = event.raw_text if hasattr(event, 'raw_text') else (
				event.message.text if hasattr(event, 'message') and hasattr(event.message, 'text') else "N/A")
		sender_id_for_report = event.sender_id if hasattr(event, 'sender_id') else "N/A"
		chat_id_for_report = event.chat_id if hasattr(event, 'chat_id') else "N/A"

		event_details_str = f"Кому: <code>{user_id}</code> (в чате <code>{chat_id_for_report}</code>)\nТриггер: {html.escape(action_details)}\nОригинал (от <code>{sender_id_for_report}</code>):\n<pre>{html.escape(event_text_for_report[:400])}</pre>"
		if hasattr(event, 'reply_to_msg_id') and event.reply_to_msg_id and is_dialogue_reply:
				event_details_str += f"\nОтвет на сообщение ID: <code>{event.reply_to_msg_id}</code>"
		response_info_str = f"<pre>{html.escape(text[:400])}</pre>"

		try:
				await event.reply(text, parse_mode='html', link_preview=False)
				sent_successfully = True
				logging.info(f"Ответ отправлен пользователю {user_id}")

				analytics_action = action_details
				if is_dialogue_reply:
						analytics_action = "dialogue_reply_sent"
				elif action_details == "dm_auto_reply":
						analytics_action = "dm_auto_reply_sent"

				add_analytics_log(account_id=current_account_id_for_log,
													action_type=analytics_action,
													details=f"To user: {user_id}, Keyword: {action_details.split(':')[-1] if 'keyword' in action_details else 'N/A'}",
													success=True)
				status_str = "✅ Успешно"
		except (UserIsBlockedError, PeerIdInvalidError) as e_user_issue:
				logging.warning(
						f"Не удалось отправить ответ пользователю {user_id} (возможно, бот заблокирован или чат не существует): {e_user_issue}")
				status_str = f"⚠️ Ошибка ({type(e_user_issue).__name__})"
				error_text_report = str(e_user_issue)
				add_analytics_log(account_id=current_account_id_for_log,
													action_type=f"{action_details}_fail" if not is_dialogue_reply else "dialogue_reply_user_error",
													details=f"Error sending to user {user_id}: {e_user_issue}", success=False)
		except Exception as e:
				logging.error(f"Ошибка при отправке ответа пользователю {user_id}: {e}")

				analytics_action_error = action_details
				if is_dialogue_reply:
						analytics_action_error = "dialogue_reply_error"
				elif action_details == "dm_auto_reply":
						analytics_action_error = "dm_auto_reply_error"

				add_analytics_log(account_id=current_account_id_for_log,
													action_type=analytics_action_error,
													details=f"Error sending to user {user_id}: {e}", success=False)
				status_str = "❌ Ошибка"
				error_text_report = str(e)

		if not sent_successfully or error_text_report:
				await send_report(
						report_title=report_title,
						status=status_str,
						account_info=account_info_str,
						event_details=event_details_str,
						response_info=response_info_str,
						error_info=error_text_report
				)


async def send_rotating_chat_reply(event, text: str, user_id, cycle_name: str, action_details: str,
																	 coordinator_account_id: int | None = None,
																	 coordinator_label: str = "N/A") -> bool:
		candidate_clients = get_accounts_ordered_for_cycle(cycle_name)
		if not candidate_clients:
				logging.warning(f"ROTATING_REPLY: no connected accounts for {cycle_name}.")
				add_analytics_log(
						account_id=coordinator_account_id,
						action_type=f"{action_details}_fail",
						details=f"No connected accounts for chat {event.chat_id}",
						success=False
				)
				return False

		delay = random.randint(REPLY_DELAY_MIN, REPLY_DELAY_MAX)
		first_candidate = candidate_clients[0][1]
		logging.info(
				f"ROTATING_REPLY: cycle={cycle_name}; coordinator={coordinator_label} (ID: {coordinator_account_id}); "
				f"first candidate={first_candidate.get('label', 'N/A')} (ID: {first_candidate.get('id')}); delay={delay}s."
		)
		await asyncio.sleep(delay)

		event_text_for_report = event.raw_text if hasattr(event, "raw_text") else "N/A"
		attempt_errors = []

		for reply_client, reply_data in candidate_clients:
				reply_account_id = reply_data.get("id")
				reply_label = reply_data.get("label", reply_data.get("session_name", "N/A"))
				try:
						await reply_client.send_message(
								entity=event.chat_id,
								message=text,
								reply_to=event.message.id,
								parse_mode="html",
								link_preview=False
						)
						record_account_cycle_success(cycle_name, reply_account_id)
						add_analytics_log(
								account_id=reply_account_id,
								action_type=action_details,
								details=f"Chat: {event.chat_id}, ReplyTo: {event.message.id}, User: {user_id}",
								success=True
						)
						logging.info(
								f"ROTATING_REPLY: account {reply_label} (ID: {reply_account_id}) sent {action_details} "
								f"in chat {event.chat_id} as reply to {event.message.id}."
						)
						return True
				except Exception as e:
						error_text = f"{reply_label} (ID: {reply_account_id}): {type(e).__name__}: {e}"
						attempt_errors.append(error_text)
						logging.warning(f"ROTATING_REPLY: send failed for {error_text}")
						add_analytics_log(
								account_id=reply_account_id,
								action_type=f"{action_details}_fail",
								details=f"Chat: {event.chat_id}, ReplyTo: {event.message.id}, Error: {type(e).__name__}: {e}",
								success=False
						)

		await send_report(
				report_title="Ответ на сообщение",
				status="❌ Ошибка",
				account_info=f"<code>{html.escape(str(coordinator_label))}</code> (ID: {coordinator_account_id if coordinator_account_id else 'N/A'})",
				event_details=(
						f"Кому: <code>{user_id}</code> (в чате <code>{event.chat_id}</code>)\n"
						f"Триггер: {html.escape(action_details)}\n"
						f"Оригинал: <pre>{html.escape(event_text_for_report[:400])}</pre>"
				),
				response_info=f"<pre>{html.escape((text or '')[:400])}</pre>",
				error_info="\n".join(attempt_errors[-10:]) if attempt_errors else "No send attempts succeeded."
		)
		return False


async def on_private_message_handler(event, client_obj, client_data):
		account_db_id = client_data.get("id")
		account_label = client_data.get("label", "N/A")

		if event.sender_id == client_data.get("user_id"):
				return

		try:
				is_dm_reply_enabled = get_config_value("dm_auto_reply_enabled", "False").lower() == 'true'
				if not is_dm_reply_enabled:
						return

				auto_reply_text = get_config_value("dm_auto_reply_message")
				if not auto_reply_text or not auto_reply_text.strip():
						logging.info(f"DM автоответчик включен, но сообщение не установлено для аккаунта {account_label}.")
						return

				logging.info(f"Получено ЛС для {account_label} от {event.sender_id}. Отправка автоответа.")

				await anti_spam_send_reply(
						event,
						auto_reply_text,
						event.chat_id,
						account_db_id,
						action_details="dm_auto_reply",
						client_label=account_label,
						is_dialogue_reply=False
				)

		except Exception as e:
				logging.error(f"[{account_label}] Ошибка в on_private_message_handler: {e}", exc_info=True)
				add_analytics_log(account_id=account_db_id, action_type="error_on_private_message", details=str(e),
													success=False)
				raw_text_for_report = event.raw_text if hasattr(event, 'raw_text') else "N/A"
				await send_report(
						report_title="Ошибка в обработчике ЛС (Автоответчик)",
						status="❌ КРИТИЧЕСКАЯ ОШИБКА",
						account_info=f"<code>{account_label}</code> (ID: {account_db_id if account_db_id else 'N/A'})",
						event_details=f"От: <code>{event.sender_id if hasattr(event, 'sender_id') else 'N/A'}</code>\nСообщение: <pre>{html.escape(raw_text_for_report[:200])}</pre>",
						response_info="Автоответ не был отправлен",
						error_info=str(e)
				)


async def on_new_message_handler(event, client_obj, client_data):
		global currently_processing_posts, processing_posts_lock
		account_db_id = client_data.get("id") if client_data else None
		account_label = client_data.get("label", "N/A") if client_data else "N/A"

		workspace_id = client_data.get("_workspace_id") if client_data else get_current_workspace_id()
		msg_key = (workspace_id, event.chat_id, event.message.id)
		lock_acquired = False

		try:
				chat_id = event.chat_id
				chat_id_variants = _chat_id_variants(chat_id)
				sender_id = event.sender_id

				conn = get_db_connection()
				cursor = conn.cursor()

				is_in_active_category_for_account = False
				category_name_for_chat = None
				if account_db_id and chat_id_variants:
						cursor.execute(f"""
								SELECT cjc.category_name FROM category_joined_chats cjc
								JOIN chat_categories cc ON cjc.category_name = cc.category_name
								WHERE cjc.chat_id IN ({_sql_placeholders(chat_id_variants)})
								AND cjc.account_db_id = ? AND cc.is_active = 1 LIMIT 1
						""", (*chat_id_variants, account_db_id))
						category_row = cursor.fetchone()
						if category_row:
								is_in_active_category_for_account = True
								category_name_for_chat = category_row["category_name"]

				processed_by_keyword = False

				subscription_mode = get_config_value("subscription_mode", "all_accounts")
				should_this_account_respond = True

				if subscription_mode == "single_account_sticky":
						assigned_account_id_for_entity = _get_group_assigned_account_id(chat_id)
						if assigned_account_id_for_entity is not None and assigned_account_id_for_entity != account_db_id:
								should_this_account_respond = False

				if not should_this_account_respond:
						logging.debug(
								f"ON_NEW_MESSAGE: Аккаунт {account_label} не назначен для чата {chat_id} в режиме single_account_sticky, пропускает ответ на ключевое слово.")
						conn.close()
						return

				lock_acquired = await _try_claim_processing_key(
						msg_key, 60, "ON_NEW_MESSAGE", account_label, account_db_id)
				if not lock_acquired:
						conn.close()
						return
				
				if is_in_active_category_for_account and category_name_for_chat:
						category_specific_keywords = get_category_keywords(category_name_for_chat)
						text_lower = event.raw_text.lower()
						for r_cat_config in category_specific_keywords:
								keyword_cat_db = r_cat_config["keyword"].lower()
								if keyword_cat_db in text_lower:
										logging.info(
												f"ON_NEW_MESSAGE: Найдено ключевое слово категории '{keyword_cat_db}' ({category_name_for_chat}) аккаунтом {account_label}")
										action_details_str_cat = f"category_keyword_reply:{category_name_for_chat}:{keyword_cat_db}"
										ans_text_cat = ""                    
										if r_cat_config["response_type"] == "predefined":
												ans_text_cat = r_cat_config["answer"]
										elif r_cat_config["response_type"] == "openai":
												ans_text_cat = await asyncio.to_thread(
														generate_vpn_comment,
														context=event.raw_text,
														current_account_id_for_log=account_db_id,
														chat_id_for_prompt=chat_id
												)

										await send_rotating_chat_reply(
												event,
												ans_text_cat,
												sender_id,
												cycle_name=f"trigger_reply_chat_{chat_id}",
												action_details=action_details_str_cat,
												coordinator_account_id=account_db_id,
												coordinator_label=account_label,
										)
										processed_by_keyword = True
										break

				if not processed_by_keyword:
						manually_added_group = None
						if chat_id_variants:
								cursor.execute(
										f"SELECT id FROM groups WHERE id IN ({_sql_placeholders(chat_id_variants)}) AND enabled = 1 LIMIT 1",
										chat_id_variants
								)
								manually_added_group = cursor.fetchone()

						listen_all_enabled = get_config_value("listen_all", "True").lower() == "true"
						should_be_active_in_chat = manually_added_group or is_in_active_category_for_account or listen_all_enabled
						can_check_global_keywords = should_be_active_in_chat
						cursor.execute("SELECT keyword, answer, response_type FROM responses")
						global_responses = cursor.fetchall()

						if can_check_global_keywords:
								text_lower_global = event.raw_text.lower()

								for r_config in global_responses:
										keyword_from_db = r_config["keyword"].lower()
										if keyword_from_db in text_lower_global:
												logging.info(
														f"ON_NEW_MESSAGE: Найдено глобальное ключевое слово '{keyword_from_db}' аккаунтом {account_label}")
												action_details_str = f"keyword_reply:{keyword_from_db}"
												ans_text_to_send = ""
												if r_config["response_type"] == "predefined":
														ans_text_to_send = r_config["answer"]
												elif r_config["response_type"] == "openai":
														ans_text_to_send = await asyncio.to_thread(
																generate_vpn_comment,
																context=event.raw_text,
																current_account_id_for_log=account_db_id,
																chat_id_for_prompt=None,
														)

												await send_rotating_chat_reply(
														event,
														ans_text_to_send,
														sender_id,
														cycle_name=f"trigger_reply_chat_{chat_id}",
														action_details=action_details_str,
														coordinator_account_id=account_db_id,
														coordinator_label=account_label,
												)
												processed_by_keyword = True
												break
						else:
								inactive_keyword = _matching_global_keyword_from_rows(global_responses, event.raw_text)
								if inactive_keyword:
										logging.info(
												"ON_NEW_MESSAGE: keyword '%s' ignored in chat %s because listen_all=False and the group/category is not enabled.",
												inactive_keyword, chat_id
										)
				conn.close()

		except Exception as e:
				logging.error(
						f"[{client_data.get('session_name', 'UnknownSession') if client_data else 'Unknown'}] on_new_message_handler: Ошибка: {e}",
						exc_info=True)
				if account_db_id: add_analytics_log(account_id=account_db_id, action_type="error_on_new_message",
																						details=str(e), success=False)
				raw_text_for_report = event.raw_text if hasattr(event, 'raw_text') else "N/A"
				await send_report(
						report_title="Ошибка в обработчике новых сообщений (не диалог)",
						status="❌ КРИТИЧЕСКАЯ ОШИБКА",
						account_info=f"<code>{account_label}</code> (ID: {account_db_id if account_db_id else 'N/A'})",
						event_details=f"В чате: <code>{event.chat_id if hasattr(event, 'chat_id') else 'N/A'}</code>\nСообщение: <pre>{html.escape(raw_text_for_report[:200])}</pre>",
						response_info="Действие не выполнено",
						error_info=str(e)
				)
		finally:
				if lock_acquired:
						await _release_processing_key(msg_key, "ON_NEW_MESSAGE", account_label, account_db_id)

async def auto_comment_on_new_topic_handler(event, client_obj, client_data):
		global currently_processing_posts, processing_posts_lock
		coordinator_account_id = client_data.get("id") if client_data else None
		coordinator_label = client_data.get("label", "Unknown") if client_data else "Unknown"

		workspace_id = client_data.get("_workspace_id") if client_data else get_current_workspace_id()
		post_key = (workspace_id, event.chat_id, event.message.id)
		lock_acquired = False

		logging.info(
				f"AUTO_COMMENT_HANDLER: coordinator {coordinator_label} (ID: {coordinator_account_id}) saw post {post_key}.")
		report_title = "Auto comment on new post"
		status_str = "Not processed"
		error_text_report = ""
		event_details_str = f"Channel: <code>{event.chat_id}</code> (post ID: {event.message.id})\nPost context:\n<pre>{html.escape(event.raw_text[:400])}</pre>"
		response_info_str = "Comment was not generated or sent"
		comment_text_for_report = "N/A"
		account_info_str = f"<code>{coordinator_label}</code> (ID: {coordinator_account_id if coordinator_account_id else 'N/A'})"
		sent_successfully = False

		try:
				conn = get_db_connection()
				c = conn.cursor()
				c.execute("SELECT id, username, title, linked_chat_id FROM channels WHERE id = ? AND enabled = 1", (event.chat_id,))
				ch_row = c.fetchone()
				conn.close()

				if not ch_row:
						logging.info(f"AUTO_COMMENT_HANDLER: channel {event.chat_id} is not enabled in DB. Skipping.")
						return

				lock_acquired = await _try_claim_processing_key(
						post_key, POST_LOCK_TIMEOUT_SECONDS, "AUTO_COMMENT_HANDLER", coordinator_label, coordinator_account_id)
				if not lock_acquired:
						return

				cycle_name = f"auto_comment_channel_{event.chat_id}"
				candidate_clients = get_accounts_ordered_for_cycle(cycle_name)
				if not candidate_clients:
						status_str = "No connected accounts"
						error_text_report = "No connected Telethon accounts are available for auto commenting."
						return

				first_candidate_data = candidate_clients[0][1]
				logging.info(
						f"AUTO_COMMENT_HANDLER: rotating accounts for channel {event.chat_id}; first candidate is {first_candidate_data.get('label', 'N/A')} (ID: {first_candidate_data.get('id')}).")
				await asyncio.sleep(random.randint(REPLY_DELAY_MIN // 2, REPLY_DELAY_MAX // 2))

				comment_text_for_report = await asyncio.to_thread(
						generate_vpn_comment,
						context=event.raw_text,
						current_account_id_for_log=first_candidate_data.get("id"),
						chat_id_for_prompt=event.chat_id
				)
				response_info_str = f"<pre>{html.escape(comment_text_for_report[:400])}</pre>"
				logging.info(
						f"AUTO_COMMENT_HANDLER: generated comment preview: '{comment_text_for_report[:50]}'")

				failure_markers = (
						"generation failed",
						"error ai",
						"lm studio generation failed",
						"\u0438\u0437\u0432\u0438\u043d",
						"\u043d\u0435 \u0441\u043c\u043e\u0433",
						"\u043d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c",
				)
				comment_text_lower = comment_text_for_report.lower() if comment_text_for_report else ""
				if not comment_text_for_report or any(marker in comment_text_lower for marker in failure_markers):
						status_str = "AI did not return a valid comment"
						error_text_report = "Comment generator returned an empty or error response."
						return

				linked_chat_id_str = ch_row["linked_chat_id"]
				attempt_errors = []
				success_account_id = None
				success_account_label = None
				success_action_type = None
				success_details = None

				for attempt_client, attempt_data in candidate_clients:
						attempt_account_id = attempt_data.get("id")
						attempt_label = attempt_data.get("label", attempt_data.get("session_name", "N/A"))
						account_info_str = f"<code>{html.escape(str(attempt_label))}</code> (ID: {attempt_account_id if attempt_account_id else 'N/A'})"
						logging.info(
								f"AUTO_COMMENT_HANDLER: trying account {attempt_label} (ID: {attempt_account_id}) for post {post_key}.")

						discussion_msg_id = None
						target_linked_chat_peer = None
						if linked_chat_id_str and str(linked_chat_id_str).strip():
								try:
										target_linked_chat_peer = _linked_chat_peer_from_db(linked_chat_id_str)

										disc_data = await attempt_client(
												GetDiscussionMessageRequest(peer=event.chat_id, msg_id=event.message.id))
										if disc_data and disc_data.messages:
												discussion_msg_id = disc_data.messages[0].id
								except Exception as e_disc:
										attempt_errors.append(f"{attempt_label}: discussion lookup failed: {e_disc}")
										logging.warning(
												f"AUTO_COMMENT_HANDLER: discussion lookup failed for {attempt_label}: {e_disc}")

						if discussion_msg_id and target_linked_chat_peer:
								try:
										await attempt_client.send_message(
												entity=target_linked_chat_peer,
												message=comment_text_for_report,
												reply_to=discussion_msg_id,
												parse_mode='html',
												link_preview=False
										)
										sent_successfully = True
										success_account_id = attempt_account_id
										success_account_label = attempt_label
										success_action_type = "auto_comment_linked_chat"
										success_details = f"Channel: {event.chat_id}, LinkedChat: {linked_chat_id_str}"
										status_str = "Sent via linked discussion chat"
										break
								except Exception as e_send_linked:
										attempt_errors.append(f"{attempt_label}: linked send failed: {e_send_linked}")
										logging.warning(
												f"AUTO_COMMENT_HANDLER: linked-chat send failed for {attempt_label}: {e_send_linked}")

						try:
								await attempt_client.send_message(
										entity=event.chat_id,
										message=comment_text_for_report,
										comment_to=event.message.id,
										parse_mode='html',
										link_preview=False
								)
								sent_successfully = True
								success_account_id = attempt_account_id
								success_account_label = attempt_label
								success_action_type = "auto_comment_channel"
								success_details = f"Channel: {event.chat_id}"
								status_str = "Sent via channel comments"
								break
						except Exception as e_send_inline:
								attempt_errors.append(f"{attempt_label}: inline send failed: {e_send_inline}")
								logging.warning(
										f"AUTO_COMMENT_HANDLER: inline comment failed for {attempt_label}: {e_send_inline}")

				if sent_successfully:
						record_account_cycle_success(cycle_name, success_account_id)
						account_info_str = f"<code>{html.escape(str(success_account_label))}</code> (ID: {success_account_id if success_account_id else 'N/A'})"
						add_analytics_log(
								account_id=success_account_id,
								action_type=success_action_type,
								details=success_details,
								success=True
						)
						record_account_runtime_event(success_account_id, "auto_comment", True)
						logging.info(
								f"AUTO_COMMENT_HANDLER: post {post_key} commented by {success_account_label} (ID: {success_account_id}).")
						try:
								append_auto_comment_report(
										channel_id=event.chat_id,
										post_id=event.message.id,
										comment_text=comment_text_for_report,
										account_id=success_account_id,
										account_label=str(success_account_label),
										channel_username=ch_row["username"],
										channel_title=ch_row["title"],
										sent_mode=status_str
								)
						except Exception as e_report_file:
								logging.error(f"AUTO_COMMENT_HANDLER: failed to write auto-comment report file: {e_report_file}")
				else:
						status_str = "Failed for all accounts"
						error_text_report = "\n".join(attempt_errors[-10:]) if attempt_errors else "No send attempts succeeded."
						add_analytics_log(
								account_id=coordinator_account_id,
								action_type="auto_comment_all_accounts_failed",
								details=f"Channel: {event.chat_id}, Errors: {len(attempt_errors)}",
								success=False
						)
						if coordinator_account_id:
								record_account_runtime_event(
										coordinator_account_id,
										"auto_comment",
										False,
										last_error=error_text_report[:500]
								)

		except Exception as e_outer:
				logging.error(
						f"[{client_data.get('session_name', 'UnknownSession')}] auto_comment_on_new_topic_handler: outer error: {e_outer}",
						exc_info=True)
				add_analytics_log(account_id=coordinator_account_id, action_type="error_auto_comment_handler", details=str(e_outer),
											success=False)
				status_str = "Handler error"
				error_text_report = str(e_outer)
				if comment_text_for_report != "N/A" and comment_text_for_report:
						response_info_str = f"<pre>{html.escape(comment_text_for_report[:400])}</pre>"
				else:
						response_info_str = "Comment was not generated before the error"
		finally:
				if lock_acquired:
						await _release_processing_key(post_key, "AUTO_COMMENT_HANDLER", coordinator_label, coordinator_account_id)

		await send_report(
				report_title=report_title,
				status=status_str,
				account_info=account_info_str,
				event_details=event_details_str,
				response_info=response_info_str,
				error_info=error_text_report
		)


async def _create_client_with_handlers(acc_row_data: dict, workspace_id: str = None) -> TelegramClient | None:
		workspace_id = workspace_id or get_current_workspace_id()
		db_id = acc_row_data["id"]
		sname = acc_row_data["session_name"]
		api_id_val = acc_row_data["api_id"]
		api_hash_val = acc_row_data["api_hash"]

		proxy_params_runtime = None
		if acc_row_data.get("proxy_ip") and acc_row_data.get("proxy_port"):
				proxy_type_str_runtime = (acc_row_data.get("proxy_type") or 'socks5').lower()
				proxy_details_runtime = {
						"proxy_type": proxy_type_str_runtime,
						"addr": acc_row_data["proxy_ip"],
						"port": int(acc_row_data["proxy_port"])
				}
				if acc_row_data.get("proxy_username"):
						proxy_details_runtime["username"] = acc_row_data["proxy_username"]
				if acc_row_data.get("proxy_password"):
						proxy_details_runtime["password"] = acc_row_data["proxy_password"]
				proxy_params_runtime = proxy_details_runtime

		session = SQLiteSession(os.path.join(get_workspace_session_dir(workspace_id), sname))
		client = TelegramClient(session, api_id_val, api_hash_val, proxy=proxy_params_runtime)

		try:
				await client.connect()
				if not await client.is_user_authorized():
						logging.warning(f"[{sname}] Аккаунт не авторизован при реинициализации. Не удалось запустить.")
						await client.disconnect()
						return None

				me = await client.get_me()
				if not me:
						logging.error(f"Не удалось получить информацию (get_me) для аккаунта {sname} при реинициализации.")
						await client.disconnect()
						return None

				current_client_data_dict = acc_row_data.copy()
				current_client_data_dict["user_id"] = me.id
				current_client_data_dict["_workspace_id"] = workspace_id

				@client.on(events.NewMessage(incoming=True))
				async def combined_message_handler(event: events.NewMessage.Event, current_client_obj=client,
																					 ccd=current_client_data_dict):
						set_workspace_context(ccd.get("_workspace_id", workspace_id))
						if event.sender_id in ALL_CLIENT_USER_IDS:
								ignored_keyword = _matching_global_keyword_from_db(getattr(event, "raw_text", None))
								if ignored_keyword:
										logging.info(
												"ON_NEW_MESSAGE: keyword '%s' ignored in chat %s because sender %s is a managed account.",
												ignored_keyword, getattr(event, "chat_id", "N/A"), event.sender_id
										)
								return

						if event.is_private:
								await on_private_message_handler(event, current_client_obj, ccd)
								return

						if event.is_reply:
								try:
										replied_to_msg = await event.get_reply_message()
										if replied_to_msg and replied_to_msg.sender_id == ccd.get("user_id"):
												await _handle_dialogue_logic(event, current_client_obj, ccd)
												return
								except Exception as e:
										logging.warning(f"Не удалось получить сообщение, на которое отвечают: {e}")

						if (event.is_channel and not getattr(event, "is_group", False)
										and event.message and not event.message.is_reply and not event.edit_date):
								if event.message.date and START_TIME and event.message.date < START_TIME:
										return
								await auto_comment_on_new_topic_handler(event, current_client_obj, ccd)
								return

						await on_new_message_handler(event, current_client_obj, ccd)

				return client
		except Exception as e:
				logging.error(f"Ошибка при создании и подключении клиента {sname} при реинициализации: {e}", exc_info=True)
				if client and client.is_connected():
						await client.disconnect()
				return None


async def _handle_dialogue_logic(event, current_client_obj, current_client_data_dict):
		global conversation_tracker
		account_db_id_dialogue = current_client_data_dict.get("id")
		account_label_dialogue = current_client_data_dict.get("label", "N/A")

		conn_check = get_db_connection()
		c_check = conn_check.cursor()
		chat_id_variants = _chat_id_variants(event.chat_id)
		is_monitored_chat = None
		try:
				if chat_id_variants:
						placeholders = _sql_placeholders(chat_id_variants)
						c_check.execute(f"""
								SELECT 1 FROM groups WHERE id IN ({placeholders}) AND enabled = 1
								UNION ALL
								SELECT 1 FROM category_joined_chats cjc
								JOIN chat_categories cc ON cjc.category_name = cc.category_name
								WHERE cjc.chat_id IN ({placeholders}) AND cc.is_active = 1
								LIMIT 1
						""", (*chat_id_variants, *chat_id_variants))
						is_monitored_chat = c_check.fetchone()
		finally:
				conn_check.close()

		if not is_monitored_chat:
				logging.info(f"Диалог в чате {event.chat_id} проигнорирован, т.к. чат не отслеживается в БД.")
				return

		if event.sender_id in ALL_CLIENT_USER_IDS or not event.is_reply:
				return
		try:
				replied_to_msg_dialogue = await event.get_reply_message()
				if not replied_to_msg_dialogue or replied_to_msg_dialogue.sender_id != current_client_data_dict.get("user_id"):
						return

				chat_id_dialogue = event.chat_id
				user_id_who_replied_dialogue = event.sender_id
				conversation_key_dialogue = (
						account_db_id_dialogue, chat_id_dialogue, replied_to_msg_dialogue.id, user_id_who_replied_dialogue)
				current_turn_count_dialogue = conversation_tracker[account_db_id_dialogue][
						conversation_key_dialogue]
				max_turns_dialogue = int(get_config_value("max_conversation_turns", "5"))

				if current_turn_count_dialogue >= max_turns_dialogue:
						logging.info(
								f"Диалог {conversation_key_dialogue} достиг {current_turn_count_dialogue}/{max_turns_dialogue} ходов для аккаунта {account_label_dialogue}. Не отвечаем.")
						conversation_tracker[account_db_id_dialogue].pop(conversation_key_dialogue, None)
						return

				dialogue_history_for_ai = []
				temp_msg = event.message
				max_dialogue_history_messages = 10
				if temp_msg and temp_msg.text:
						dialogue_history_for_ai.append({"role": "user", "content": temp_msg.text})
				current_reply_to_id = temp_msg.reply_to_msg_id
				for _ in range(max_dialogue_history_messages - 1):
						if not current_reply_to_id: break
						try:
								prev_msg = await current_client_obj.get_messages(chat_id_dialogue,
																																 ids=current_reply_to_id)
								if not prev_msg or not prev_msg.text: break
								role = "assistant" if prev_msg.sender_id == current_client_data_dict.get("user_id") else "user"
								if prev_msg.sender_id == current_client_data_dict.get(
												"user_id") or prev_msg.sender_id == user_id_who_replied_dialogue:
										dialogue_history_for_ai.append({"role": role, "content": prev_msg.text})
								if not prev_msg.is_reply: break
								current_reply_to_id = prev_msg.reply_to_msg_id
						except Exception as e_hist:
								logging.warning(f"Ошибка при сборе истории диалога: {e_hist}");
								break
				dialogue_history_for_ai.reverse()
				if not dialogue_history_for_ai or (
								dialogue_history_for_ai and dialogue_history_for_ai[-1]["role"] == "assistant" and not (
								event.message and event.message.text)):
						if not dialogue_history_for_ai and event.message and event.message.text and replied_to_msg_dialogue and replied_to_msg_dialogue.text:
								dialogue_history_for_ai = [{"role": "assistant", "content": replied_to_msg_dialogue.text},
																					 {"role": "user", "content": event.message.text}]
						else:
								logging.warning(
										f"Не удалось собрать корректный начальный контекст для диалога {conversation_key_dialogue}.");
								return

				ai_reply_text_dialogue = await asyncio.to_thread(generate_vpn_comment,
																												 current_account_id_for_log=account_db_id_dialogue,
																												 chat_id_for_prompt=chat_id_dialogue,
																												 dialogue_history=dialogue_history_for_ai)
				if ai_reply_text_dialogue:
						conversation_tracker[account_db_id_dialogue][
								conversation_key_dialogue] = current_turn_count_dialogue + 1
						await anti_spam_send_reply(event, ai_reply_text_dialogue, user_id_who_replied_dialogue,
																			 account_db_id_dialogue,
																			 action_details=f"dialogue_turn:{current_turn_count_dialogue + 1}",
																			 client_label=account_label_dialogue, is_dialogue_reply=True)
				else:
						logging.warning(
								f"AI не смог сгенерировать ответ в диалоге для {conversation_key_dialogue} аккаунтом {account_label_dialogue}")
						await send_report(report_title="Ответ в диалоге", status="⚠️ AI не вернул текст",
															account_info=f"<code>{account_label_dialogue}</code> (ID: {account_db_id_dialogue})",
															event_details=f"Пользователю: <code>{user_id_who_replied_dialogue}</code> в чате <code>{chat_id_dialogue}</code>.\nСообщение: <pre>{html.escape(event.message.text[:200])}</pre>",
															response_info="Попытка диалога, но AI не сгенерировал ответ.")
		except Exception as e_dialogue_main:
				logging.error(
						f"Ошибка в обработчике диалога для аккаунта {account_label_dialogue} в чате {event.chat_id if event else 'N/A'}: {e_dialogue_main}",
						exc_info=True)
				raw_text_dialogue_err = event.message.text if hasattr(event, 'message') and hasattr(event.message,
																																														'text') else 'N/A'
				await send_report(report_title="Ошибка в обработчике диалога", status="❌ КРИТИЧЕСКАЯ ОШИБКА",
													account_info=f"<code>{account_label_dialogue}</code> (ID: {account_db_id_dialogue})",
													event_details=f"Чат: <code>{event.chat_id if event else 'N/A'}</code>, Пользователь: <code>{event.sender_id if event else 'N/A'}</code>\nСообщение: <pre>{html.escape(raw_text_dialogue_err[:200])}</pre>",
													response_info="Действие не выполнено", error_info=str(e_dialogue_main))


async def _run_client_until_disconnected_in_workspace(client: TelegramClient, workspace_id: str):
		token = set_workspace_context(workspace_id)
		try:
				await client.run_until_disconnected()
		finally:
				reset_workspace_context(token)


async def attach_authorized_client_to_runtime(account_db_id: int, client: TelegramClient, account_data: dict,
																							user_id: int | None = None, workspace_id: str = None):
		workspace_id = workspace_id or get_current_workspace_id()
		token = set_workspace_context(workspace_id)
		try:
				await remove_client_from_runtime(account_db_id, account_data.get("session_name", str(account_db_id)))
		finally:
				reset_workspace_context(token)

		runtime_data = account_data.copy()
		runtime_data["id"] = account_db_id
		runtime_data["user_id"] = user_id
		runtime_data["_workspace_id"] = workspace_id

		workspace_clients.setdefault(workspace_id, []).append((client, runtime_data))
		if user_id:
				workspace_user_ids.setdefault(workspace_id, set()).add(user_id)
		task = asyncio.create_task(_run_client_until_disconnected_in_workspace(client, workspace_id))
		workspace_client_tasks.setdefault(workspace_id, {})[account_db_id] = task
		conversation_tracker[account_db_id] = defaultdict(int)
		_sync_legacy_runtime_refs()
		logging.info(f"Client for account ID {account_db_id} attached to workspace {workspace_id} runtime.")
		return task


async def remove_client_from_runtime(account_db_id: int, session_name_for_log: str):
		workspace_id = get_current_workspace_id()
		runtime_clients = workspace_clients.setdefault(workspace_id, [])
		runtime_tasks = workspace_client_tasks.setdefault(workspace_id, {})
		runtime_user_ids = workspace_user_ids.setdefault(workspace_id, set())

		client_to_disconnect = None
		user_id_to_remove = None
		for index, (client_obj, data) in enumerate(list(runtime_clients)):
				if data.get("id") == account_db_id:
						client_to_disconnect = client_obj
						user_id_to_remove = data.get("user_id")
						runtime_clients.pop(index)
						break

		task = runtime_tasks.pop(account_db_id, None)
		if task and not task.done():
				task.cancel()
				try:
						await task
				except asyncio.CancelledError:
						pass
				except Exception as e:
						logging.debug(f"Client task {workspace_id}:{account_db_id} stopped with error: {e}")

		if user_id_to_remove:
				runtime_user_ids.discard(user_id_to_remove)

		if client_to_disconnect and client_to_disconnect.is_connected():
				await client_to_disconnect.disconnect()

		_sync_legacy_runtime_refs()
		logging.info(f"Client ID {account_db_id} ({session_name_for_log}) removed from workspace {workspace_id}.")


async def reinitialize_telethon_client(account_db_id: int) -> tuple[bool, str]:
		workspace_id = get_current_workspace_id()
		account_data_from_db = get_account_details(account_db_id)
		if not account_data_from_db:
				return False, "Account not found in this workspace database."
		if not int(account_data_from_db.get("is_enabled", 1) or 0):
				return False, "Account is disabled in this workspace."

		proxy_block_reason = get_account_proxy_block_reason(account_data_from_db)
		if proxy_block_reason:
				await remove_client_from_runtime(account_db_id, account_data_from_db.get("session_name", str(account_db_id)))
				add_analytics_log(
						account_id=account_db_id,
						action_type="client_start_blocked_proxy",
						details=proxy_block_reason,
						success=False
				)
				return False, f"Client was not started because {proxy_block_reason}."

		await remove_client_from_runtime(account_db_id, account_data_from_db.get("session_name", str(account_db_id)))

		new_client = await _create_client_with_handlers(account_data_from_db, workspace_id)
		if not new_client:
				return False, "Could not restart Telethon client."

		me = await new_client.get_me()
		if not me:
				await new_client.disconnect()
				return False, "Could not read account profile after restart."

		updated_client_data_dict = account_data_from_db.copy()
		updated_client_data_dict["user_id"] = me.id
		updated_client_data_dict["_workspace_id"] = workspace_id

		workspace_clients.setdefault(workspace_id, []).append((new_client, updated_client_data_dict))
		workspace_user_ids.setdefault(workspace_id, set()).add(me.id)
		task = asyncio.create_task(_run_client_until_disconnected_in_workspace(new_client, workspace_id))
		workspace_client_tasks.setdefault(workspace_id, {})[account_db_id] = task
		_sync_legacy_runtime_refs()

		logging.info(f"Client for account ID {account_db_id} restarted in workspace {workspace_id}.")
		return True, f"Client {updated_client_data_dict.get('label', account_db_id)} restarted."


async def stop_workspace_clients(workspace_id: str):
		token = set_workspace_context(workspace_id)
		try:
				runtime_tasks = workspace_client_tasks.setdefault(workspace_id, {})
				for account_id, task in list(runtime_tasks.items()):
						if task and not task.done():
								task.cancel()
								try:
										await task
								except asyncio.CancelledError:
										pass
								except Exception as e:
										logging.debug(f"Client task {workspace_id}:{account_id} stopped with error: {e}")
				runtime_tasks.clear()

				for client_obj, client_data in list(workspace_clients.get(workspace_id, [])):
						try:
								if client_obj and client_obj.is_connected():
										await client_obj.disconnect()
						except Exception as e:
								logging.warning(f"Failed to disconnect client {workspace_id}:{client_data.get('session_name')}: {e}")

				workspace_clients[workspace_id] = []
				workspace_user_ids[workspace_id] = set()
				_sync_legacy_runtime_refs()
		finally:
				reset_workspace_context(token)


async def stop_all_clients():
		for workspace in list_workspaces():
				await stop_workspace_clients(workspace["id"])
		_sync_legacy_runtime_refs()


async def restart_workspace_clients(workspace_id: str = None):
		workspace_id = workspace_id or get_current_workspace_id()
		await stop_workspace_clients(workspace_id)
		return await start_workspace_clients(workspace_id)


async def restart_all_clients():
		await stop_all_clients()
		return await start_all_clients()


async def _ensure_cleanup_post_locks_task():
		global cleanup_post_locks_task
		if cleanup_post_locks_task is None or cleanup_post_locks_task.done():
				cleanup_post_locks_task = asyncio.create_task(cleanup_stale_post_locks())
				logging.info("Post-lock cleanup task started.")


async def start_workspace_clients(workspace_id: str):
		token = set_workspace_context(workspace_id)
		try:
				await _ensure_cleanup_post_locks_task()
				update_all_tables()
				refresh_expired_proxies()
				init_listen_all()

				existing_tasks = workspace_client_tasks.setdefault(workspace_id, {})
				if any(task and not task.done() for task in existing_tasks.values()):
						logging.info(f"Workspace {workspace_id} clients are already running.")
						return [task for task in existing_tasks.values() if task and not task.done()]

				conn = get_db_connection()
				c = conn.cursor()
				accs_from_db = c.execute(
						"SELECT id, session_name, phone, api_id, api_hash, label, user_id, proxy_type, proxy_ip, proxy_port, proxy_username, proxy_password, is_enabled FROM accounts WHERE COALESCE(is_enabled, 1) = 1"
				).fetchall()
				conn.close()

				active_clients_temp = []
				active_tasks_temp = {}
				active_user_ids_temp = set()

				if not accs_from_db:
						logging.warning(f"No enabled accounts to start in workspace {workspace_id}.")
						workspace_clients[workspace_id] = []
						workspace_client_tasks[workspace_id] = {}
						workspace_user_ids[workspace_id] = set()
						_sync_legacy_runtime_refs()
						return []

				for acc_row in accs_from_db:
						db_id = acc_row["id"]
						sname = acc_row["session_name"]
						if not all([sname, acc_row["api_id"], acc_row["api_hash"]]):
								logging.error(f"Skipping account ID={db_id} in workspace {workspace_id}: missing session/api data.")
								continue

						client_data_dict = dict(acc_row)
						client_data_dict["_workspace_id"] = workspace_id
						proxy_block_reason = get_account_proxy_block_reason(client_data_dict)
						if proxy_block_reason:
								logging.warning(
										f"Skipping account ID={db_id} in workspace {workspace_id}: {proxy_block_reason}."
								)
								add_analytics_log(
										account_id=db_id,
										action_type="client_start_blocked_proxy",
										details=proxy_block_reason,
										success=False
								)
								continue
						conversation_tracker[db_id] = defaultdict(int)

						new_client = await _create_client_with_handlers(client_data_dict, workspace_id)
						if not new_client:
								add_analytics_log(account_id=db_id, action_type="client_start_fail", details="Client creation/auth failed", success=False)
								continue

						me = await new_client.get_me()
						if not me:
								await new_client.disconnect()
								add_analytics_log(account_id=db_id, action_type="client_start_fail", details="get_me failed", success=False)
								continue

						client_data_dict["user_id"] = me.id
						active_user_ids_temp.add(me.id)
						if acc_row["user_id"] != me.id:
								conn_update = get_db_connection()
								cur_update = conn_update.cursor()
								cur_update.execute("UPDATE accounts SET user_id = ? WHERE id = ?", (me.id, db_id))
								conn_update.commit()
								conn_update.close()

						logging.info(
								f"[start_workspace_clients] workspace={workspace_id} account={client_data_dict.get('label')} DB_ID={db_id} TG_UID={me.id}")
						add_analytics_log(account_id=db_id, action_type="client_start_success", details=f"TG_UID: {me.id}", success=True)

						task = asyncio.create_task(_run_client_until_disconnected_in_workspace(new_client, workspace_id))
						active_tasks_temp[db_id] = task
						active_clients_temp.append((new_client, client_data_dict))

				workspace_clients[workspace_id] = active_clients_temp
				workspace_client_tasks[workspace_id] = active_tasks_temp
				workspace_user_ids[workspace_id] = active_user_ids_temp
				_sync_legacy_runtime_refs()

				if not active_clients_temp:
						logging.warning(f"No clients were successfully started in workspace {workspace_id}.")

				return list(active_tasks_temp.values())
		finally:
				reset_workspace_context(token)


async def start_all_clients():
		all_tasks = []
		for workspace in list_workspaces():
				if not workspace.get("is_running", True):
						logging.info(f"Workspace {workspace.get('id')} is disabled for runtime; skipping client start.")
						continue
				all_tasks.extend(await start_workspace_clients(workspace["id"]))
		_sync_legacy_runtime_refs()
		return all_tasks


if __name__ == '__main__':
		loop = asyncio.get_event_loop()
		logging.info("Запуск userbot в автономном режиме...")
		try:
				tasks_to_run = loop.run_until_complete(start_all_clients())
				if tasks_to_run:
						logging.info(f"Всего запущено клиентских задач: {len(tasks_to_run)}")
						loop.run_forever()
				else:
						logging.warning("Не удалось запустить ни одного клиента.")
		except KeyboardInterrupt:
				logging.info("Приложение останавливается...")
		finally:
				logging.info("Закрытие цикла событий.")
				for task_id, task_obj in client_tasks.items():
						if task_obj and not task_obj.done():
								task_obj.cancel()

				for client_obj, client_d in clients:
						if client_obj and client_obj.is_connected():
								try:
										if loop.is_running():
												loop.run_until_complete(client_obj.disconnect())
										else:
												s_name_fin = client_d.get("session_name", "unknown_session")
												logging.warning(f"Loop closed, attempting direct disconnect for {s_name_fin}")
								except Exception as e_disc:
										logging.error(
												f"Error during final disconnect for {client_d.get('session_name', 'unknown_session')}: {e_disc}")
				if not loop.is_closed():
						loop.close()
