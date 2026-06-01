import logging
import contextvars
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "data/database.sqlite")
WORKSPACES_DIR = os.getenv("WORKSPACES_DIR", "data/workspaces")
WORKSPACES_INDEX_PATH = os.path.join(WORKSPACES_DIR, "workspaces.json")
DEFAULT_WORKSPACE_ID = "default"
DB_MIGRATIONS = (
	(1, "runtime_indexes"),
	(2, "openai_only_provider"),
	(3, "account_entity_memberships"),
	(4, "telegram_api_credentials"),
	(5, "advanced_triggers"),
	(6, "trigger_reply_threads"),
)
_current_workspace_id = contextvars.ContextVar("current_workspace_id", default=None)


def _default_workspace() -> dict:
	return {
		"id": DEFAULT_WORKSPACE_ID,
		"name": "Основное",
		"db_path": DB_PATH,
		"session_dir": "sessions",
		"is_running": True,
	}


def _save_workspace_index(index: dict):
	os.makedirs(WORKSPACES_DIR, exist_ok=True)
	with open(WORKSPACES_INDEX_PATH, "w", encoding="utf-8") as f:
		json.dump(index, f, ensure_ascii=False, indent=2)


def _load_workspace_index() -> dict:
	default_index = {"active": DEFAULT_WORKSPACE_ID, "workspaces": [_default_workspace()]}
	if not os.path.exists(WORKSPACES_INDEX_PATH):
		_save_workspace_index(default_index)
		return default_index
	try:
		with open(WORKSPACES_INDEX_PATH, "r", encoding="utf-8") as f:
			index = json.load(f)
	except Exception as e:
		logging.error(f"Failed to read workspaces index, recreating default: {e}")
		_save_workspace_index(default_index)
		return default_index

	changed = False
	workspaces = index.get("workspaces") or []
	if not any(ws.get("id") == DEFAULT_WORKSPACE_ID for ws in workspaces):
		workspaces.insert(0, _default_workspace())
		changed = True
	for workspace in workspaces:
		if "is_running" not in workspace:
			workspace["is_running"] = True
			changed = True
	index["workspaces"] = workspaces
	if not index.get("active") or not any(ws.get("id") == index.get("active") for ws in workspaces):
		index["active"] = DEFAULT_WORKSPACE_ID
		changed = True
	if changed:
		_save_workspace_index(index)
	return index


def list_workspaces() -> list[dict]:
	return list(_load_workspace_index().get("workspaces", []))


def get_workspace_by_id(workspace_id: str = None) -> dict:
	workspace_id = workspace_id or DEFAULT_WORKSPACE_ID
	for workspace in list_workspaces():
		if workspace.get("id") == workspace_id:
			return workspace
	if workspace_id == DEFAULT_WORKSPACE_ID:
		return _default_workspace()
	raise ValueError("Workspace not found.")


def get_active_workspace() -> dict:
	index = _load_workspace_index()
	active_id = index.get("active", DEFAULT_WORKSPACE_ID)
	for workspace in index.get("workspaces", []):
		if workspace.get("id") == active_id:
			return workspace
	return _default_workspace()


def get_active_workspace_id() -> str:
	return get_active_workspace().get("id", DEFAULT_WORKSPACE_ID)


def get_active_workspace_name() -> str:
	return get_active_workspace().get("name", "Основное")


def get_current_workspace_id() -> str:
	return _current_workspace_id.get() or get_active_workspace_id()


def get_current_workspace() -> dict:
	return get_workspace_by_id(get_current_workspace_id())


def get_current_workspace_name() -> str:
	return get_current_workspace().get("name", "Основное")


def set_workspace_context(workspace_id: str):
	get_workspace_by_id(workspace_id)
	return _current_workspace_id.set(workspace_id)


def reset_workspace_context(token):
	_current_workspace_id.reset(token)


async def run_in_workspace(workspace_id: str, awaitable):
	token = set_workspace_context(workspace_id)
	try:
		return await awaitable
	finally:
		reset_workspace_context(token)


def get_current_db_path() -> str:
	db_path = get_current_workspace().get("db_path") or DB_PATH
	parent = os.path.dirname(db_path)
	if parent:
		os.makedirs(parent, exist_ok=True)
	return db_path


def get_workspace_session_dir(workspace_id: str = None) -> str:
	workspace = None
	if workspace_id:
		workspace = get_workspace_by_id(workspace_id)
	else:
		workspace = get_current_workspace()
	session_dir = (workspace or _default_workspace()).get("session_dir") or "sessions"
	os.makedirs(session_dir, exist_ok=True)
	return session_dir


def create_workspace(name: str) -> dict:
	clean_name = (name or "").strip()
	if not clean_name:
		raise ValueError("Workspace name is empty.")
	workspace_id = f"ws_{uuid.uuid4().hex[:10]}"
	workspace = {
		"id": workspace_id,
		"name": clean_name,
		"db_path": os.path.join(WORKSPACES_DIR, workspace_id, "database.sqlite"),
		"session_dir": os.path.join("sessions", "workspaces", workspace_id),
		"is_running": True,
	}
	index = _load_workspace_index()
	index["workspaces"].append(workspace)
	index["active"] = workspace_id
	_save_workspace_index(index)
	update_all_tables()
	return workspace


def set_workspace_running(workspace_id: str, is_running: bool) -> dict:
	index = _load_workspace_index()
	for workspace in index.get("workspaces", []):
		if workspace.get("id") == workspace_id:
			workspace["is_running"] = bool(is_running)
			_save_workspace_index(index)
			return workspace
	raise ValueError("Workspace not found.")


def switch_workspace(workspace_id: str) -> dict:
	index = _load_workspace_index()
	for workspace in index.get("workspaces", []):
		if workspace.get("id") == workspace_id:
			index["active"] = workspace_id
			_save_workspace_index(index)
			update_all_tables()
			return workspace
	raise ValueError("Workspace not found.")


def delete_workspace(workspace_id: str) -> bool:
	if workspace_id == DEFAULT_WORKSPACE_ID:
		raise ValueError("Default workspace cannot be deleted.")
	index = _load_workspace_index()
	workspace_to_delete = None
	remaining = []
	for workspace in index.get("workspaces", []):
		if workspace.get("id") == workspace_id:
			workspace_to_delete = workspace
		else:
			remaining.append(workspace)
	if not workspace_to_delete:
		raise ValueError("Workspace not found.")
	index["workspaces"] = remaining
	if index.get("active") == workspace_id:
		index["active"] = DEFAULT_WORKSPACE_ID
	_save_workspace_index(index)
	for path in (workspace_to_delete.get("db_path"), workspace_to_delete.get("session_dir")):
		if not path or path in (DB_PATH, "sessions"):
			continue
		try:
			if os.path.isdir(path):
				shutil.rmtree(path)
			elif os.path.exists(path):
				os.remove(path)
		except Exception as e:
			logging.warning(f"Failed to remove workspace path {path}: {e}")
	update_all_tables()
	return True


def get_db_connection():
	conn = sqlite3.connect(get_current_db_path())
	conn.row_factory = sqlite3.Row
	return conn


@contextmanager
def db_transaction():
	conn = get_db_connection()
	try:
		yield conn, conn.cursor()
		conn.commit()
	except Exception:
		conn.rollback()
		raise
	finally:
		conn.close()


def create_tables():
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
	CREATE TABLE IF NOT EXISTS schema_migrations (
		version INTEGER PRIMARY KEY,
		name TEXT NOT NULL,
		applied_at TEXT NOT NULL
	)
	""")
	c.execute("""
	CREATE TABLE IF NOT EXISTS accounts (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		session_name TEXT UNIQUE,
		phone TEXT,
		api_id INTEGER,
		api_hash TEXT,
		label TEXT,
		user_id INTEGER,
		proxy_type TEXT,
		proxy_ip TEXT,
		proxy_port INTEGER,
		proxy_username TEXT,
		proxy_password TEXT,
		is_enabled INTEGER DEFAULT 1
	)
	""")
	c.execute("""
	CREATE TABLE IF NOT EXISTS groups (
		id INTEGER PRIMARY KEY,
		username TEXT,
		title TEXT,
		enabled INTEGER DEFAULT 1,
		assigned_account_id INTEGER DEFAULT NULL REFERENCES accounts(id) ON DELETE SET NULL
	)
	""")
	c.execute("""
	CREATE TABLE IF NOT EXISTS channels (
		id INTEGER PRIMARY KEY,
		username TEXT,
		title TEXT,
		enabled INTEGER DEFAULT 1,
		linked_chat_id TEXT,
		assigned_account_id INTEGER DEFAULT NULL REFERENCES accounts(id) ON DELETE SET NULL
	)
	""")
	c.execute("""
	CREATE TABLE IF NOT EXISTS responses (
		keyword TEXT PRIMARY KEY,
		answer TEXT,
		response_type TEXT
	)
	""")
	c.execute("""
	CREATE TABLE IF NOT EXISTS config (
		key TEXT PRIMARY KEY,
		value TEXT
	)
	""")
	c.execute("""
	CREATE TABLE IF NOT EXISTS cycle_state (
		cycle_name TEXT PRIMARY KEY,
		last_account_index INTEGER
	)
	""")
	c.execute("""
	CREATE TABLE IF NOT EXISTS analytics_log (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
		account_id INTEGER,
		action_type TEXT,
		details TEXT,
		success INTEGER,
		FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS account_health (
		account_id INTEGER PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
		status TEXT DEFAULT 'unknown',
		last_checked_at TEXT,
		details TEXT,
		telegram_user_id INTEGER,
		is_authorized INTEGER DEFAULT 0
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS account_runtime_state (
		account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
		action_type TEXT NOT NULL,
		day_key TEXT,
		attempts_today INTEGER DEFAULT 0,
		successes_today INTEGER DEFAULT 0,
		failures_today INTEGER DEFAULT 0,
		last_action_at TEXT,
		last_success_at TEXT,
		last_failure_at TEXT,
		cooldown_until TEXT,
		last_error TEXT,
		PRIMARY KEY (account_id, action_type)
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS telegram_api_credentials (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		api_id INTEGER NOT NULL,
		api_hash TEXT NOT NULL,
		label TEXT,
		max_accounts INTEGER DEFAULT 10,
		is_active INTEGER DEFAULT 1,
		created_at TEXT NOT NULL,
		updated_at TEXT NOT NULL,
		UNIQUE(api_id, api_hash)
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS bot_error_log (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		timestamp TEXT NOT NULL,
		level TEXT NOT NULL,
		source TEXT,
		message TEXT NOT NULL,
		details TEXT,
		account_id INTEGER,
		FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE SET NULL
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS health_check_runs (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		check_type TEXT NOT NULL,
		started_at TEXT NOT NULL,
		finished_at TEXT,
		status TEXT,
		details TEXT
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS chat_categories (
		category_name TEXT PRIMARY KEY,
		is_active INTEGER DEFAULT 0,
		prompt TEXT,
		chats_to_join_count INTEGER DEFAULT 5,
		join_intensity_per_hour INTEGER DEFAULT 10,
		regular_comment_enabled INTEGER DEFAULT 0,
		regular_comment_interval_minutes INTEGER DEFAULT 60,
		regular_comment_prompt TEXT
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS category_joined_chats (
		category_name TEXT NOT NULL,
		chat_id INTEGER NOT NULL,
		account_db_id INTEGER NOT NULL,
		chat_link TEXT,
		PRIMARY KEY (category_name, chat_id, account_db_id),
		FOREIGN KEY (category_name) REFERENCES chat_categories(category_name) ON DELETE CASCADE,
		FOREIGN KEY (account_db_id) REFERENCES accounts(id) ON DELETE CASCADE
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS account_entity_memberships (
		account_id INTEGER NOT NULL,
		entity_type TEXT NOT NULL CHECK(entity_type IN ('channel', 'group')),
		entity_id INTEGER NOT NULL,
		identifier TEXT,
		status TEXT DEFAULT 'active',
		joined_at TEXT NOT NULL,
		updated_at TEXT NOT NULL,
		PRIMARY KEY (account_id, entity_type, entity_id),
		FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS category_keywords (
		category_name TEXT NOT NULL,
		keyword TEXT NOT NULL,
		answer TEXT,
		response_type TEXT NOT NULL,
		PRIMARY KEY (category_name, keyword),
		FOREIGN KEY (category_name) REFERENCES chat_categories(category_name) ON DELETE CASCADE
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS trigger_intents (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		name TEXT UNIQUE NOT NULL,
		description TEXT,
		response_type TEXT NOT NULL DEFAULT 'openai',
		answer TEXT,
		is_active INTEGER DEFAULT 1,
		semantic_enabled INTEGER DEFAULT 1,
		created_at TEXT NOT NULL,
		updated_at TEXT NOT NULL
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS trigger_intent_phrases (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		intent_id INTEGER NOT NULL,
		phrase TEXT NOT NULL,
		match_type TEXT NOT NULL DEFAULT 'phrase',
		is_active INTEGER DEFAULT 1,
		created_at TEXT NOT NULL,
		updated_at TEXT NOT NULL,
		UNIQUE(intent_id, phrase),
		FOREIGN KEY (intent_id) REFERENCES trigger_intents(id) ON DELETE CASCADE
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS trigger_reply_threads (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		chat_id INTEGER NOT NULL,
		source_user_id INTEGER NOT NULL,
		source_message_id INTEGER NOT NULL,
		bot_account_id INTEGER,
		bot_user_id INTEGER,
		bot_message_id INTEGER NOT NULL,
		trigger_details TEXT,
		followup_sent INTEGER DEFAULT 0,
		created_at TEXT NOT NULL,
		updated_at TEXT NOT NULL,
		UNIQUE(chat_id, bot_message_id)
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS templates (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		templates TEXT NOT NULL
	)
	""")

	c.execute("""
	CREATE TABLE IF NOT EXISTS openai_api_keys (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		api_key TEXT UNIQUE NOT NULL,
		is_active INTEGER DEFAULT 1,
		last_used_timestamp DATETIME,
		last_failed_timestamp DATETIME,
		failure_count INTEGER DEFAULT 0,
		custom_label TEXT
	)
	""")
	c.execute("""
		CREATE TABLE IF NOT EXISTS sessions (
			session_id TEXT PRIMARY KEY,
			dc_id INTEGER NOT NULL,
			server_address TEXT,
			port INTEGER,
			auth_key BLOB NOT NULL,
			takeout_id INTEGER
		)
	""")
	c.execute("""
		CREATE TABLE IF NOT EXISTS proxies (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			proxy_type TEXT DEFAULT 'socks5',
			proxy_ip TEXT NOT NULL,
			proxy_port INTEGER NOT NULL,
			proxy_username TEXT,
			proxy_password TEXT,
			assigned_account_id INTEGER UNIQUE REFERENCES accounts(id) ON DELETE SET NULL,
			status TEXT DEFAULT 'active',
			expires_at TEXT,
			last_checked_at TEXT,
			last_error TEXT,
			UNIQUE(proxy_ip, proxy_port)
		)
	""")
	conn.commit()
	conn.close()


def _add_column_if_not_exists(table_name, column_name, column_def):
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute(f"PRAGMA table_info({table_name})")
		columns = [row['name'] for row in c.fetchall()]
		if column_name not in columns:
			c.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
			logging.info(f"Added column {column_name} to table {table_name}")
		else:
			logging.debug(f"Column {column_name} already exists in table {table_name}")
	except Exception as e:
		logging.error(f"Error adding column {column_name} to {table_name}: {e}")
	finally:
		conn.commit()
		conn.close()


def _migration_runtime_indexes(cursor):
	cursor.execute("CREATE INDEX IF NOT EXISTS idx_accounts_enabled ON accounts(is_enabled)")
	cursor.execute("CREATE INDEX IF NOT EXISTS idx_proxies_assignment ON proxies(assigned_account_id)")
	cursor.execute("CREATE INDEX IF NOT EXISTS idx_groups_enabled ON groups(enabled)")
	cursor.execute("CREATE INDEX IF NOT EXISTS idx_channels_enabled ON channels(enabled)")
	cursor.execute("CREATE INDEX IF NOT EXISTS idx_analytics_account_action ON analytics_log(account_id, action_type)")
	cursor.execute("CREATE INDEX IF NOT EXISTS idx_category_joined_chat_id ON category_joined_chats(chat_id)")


def _migration_openai_only_provider(cursor):
	cursor.execute(
		"INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
		("ai_provider", "openai")
	)
	cursor.execute("DELETE FROM config WHERE key = ?", ("g4f_model",))


def _migration_account_entity_memberships(cursor):
	now = datetime.now(timezone.utc).isoformat(timespec="seconds")
	cursor.execute("""
		CREATE TABLE IF NOT EXISTS account_entity_memberships (
			account_id INTEGER NOT NULL,
			entity_type TEXT NOT NULL CHECK(entity_type IN ('channel', 'group')),
			entity_id INTEGER NOT NULL,
			identifier TEXT,
			status TEXT DEFAULT 'active',
			joined_at TEXT NOT NULL,
			updated_at TEXT NOT NULL,
			PRIMARY KEY (account_id, entity_type, entity_id),
			FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
		)
	""")
	cursor.execute("""
		INSERT OR IGNORE INTO account_entity_memberships (
			account_id, entity_type, entity_id, identifier, status, joined_at, updated_at
		)
		SELECT assigned_account_id, 'channel', id, COALESCE(username, CAST(id AS TEXT)), 'active', ?, ?
		FROM channels
		WHERE assigned_account_id IS NOT NULL
	""", (now, now))
	cursor.execute("""
		INSERT OR IGNORE INTO account_entity_memberships (
			account_id, entity_type, entity_id, identifier, status, joined_at, updated_at
		)
		SELECT assigned_account_id, 'group', id, COALESCE(username, CAST(id AS TEXT)), 'active', ?, ?
		FROM groups
		WHERE assigned_account_id IS NOT NULL
	""", (now, now))
	cursor.execute("""
		INSERT OR IGNORE INTO account_entity_memberships (
			account_id, entity_type, entity_id, identifier, status, joined_at, updated_at
		)
		SELECT account_db_id, 'group', chat_id, chat_link, 'active', ?, ?
		FROM category_joined_chats
	""", (now, now))
	cursor.execute(
		"CREATE INDEX IF NOT EXISTS idx_entity_memberships_account_status "
		"ON account_entity_memberships(account_id, status)"
	)
	cursor.execute(
		"CREATE INDEX IF NOT EXISTS idx_entity_memberships_entity "
		"ON account_entity_memberships(entity_type, entity_id, status)"
	)


def _migration_telegram_api_credentials(cursor):
	now = datetime.now(timezone.utc).isoformat(timespec="seconds")
	cursor.execute("""
		CREATE TABLE IF NOT EXISTS telegram_api_credentials (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			api_id INTEGER NOT NULL,
			api_hash TEXT NOT NULL,
			label TEXT,
			max_accounts INTEGER DEFAULT 10,
			is_active INTEGER DEFAULT 1,
			created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL,
			UNIQUE(api_id, api_hash)
		)
	""")
	cursor.execute("""
		INSERT OR IGNORE INTO telegram_api_credentials (
			api_id, api_hash, label, max_accounts, is_active, created_at, updated_at
		)
		SELECT DISTINCT api_id, api_hash, 'API ' || api_id, 10, 1, ?, ?
		FROM accounts
		WHERE api_id IS NOT NULL
		  AND api_hash IS NOT NULL
		  AND api_hash != ''
	""", (now, now))
	cursor.execute(
		"CREATE INDEX IF NOT EXISTS idx_telegram_api_credentials_active "
		"ON telegram_api_credentials(is_active, api_id)"
	)
	cursor.execute(
		"CREATE INDEX IF NOT EXISTS idx_accounts_api_pair "
		"ON accounts(api_id, api_hash)"
	)


def _migration_advanced_triggers(cursor):
	now = datetime.now(timezone.utc).isoformat(timespec="seconds")
	cursor.execute("""
		CREATE TABLE IF NOT EXISTS trigger_intents (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			name TEXT UNIQUE NOT NULL,
			description TEXT,
			response_type TEXT NOT NULL DEFAULT 'openai',
			answer TEXT,
			is_active INTEGER DEFAULT 1,
			semantic_enabled INTEGER DEFAULT 1,
			created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL
		)
	""")
	cursor.execute("""
		CREATE TABLE IF NOT EXISTS trigger_intent_phrases (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			intent_id INTEGER NOT NULL,
			phrase TEXT NOT NULL,
			match_type TEXT NOT NULL DEFAULT 'phrase',
			is_active INTEGER DEFAULT 1,
			created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL,
			UNIQUE(intent_id, phrase),
			FOREIGN KEY (intent_id) REFERENCES trigger_intents(id) ON DELETE CASCADE
		)
	""")
	cursor.execute("CREATE INDEX IF NOT EXISTS idx_trigger_intents_active ON trigger_intents(is_active, semantic_enabled)")
	cursor.execute("CREATE INDEX IF NOT EXISTS idx_trigger_intent_phrases_intent ON trigger_intent_phrases(intent_id, is_active)")
	cursor.execute("""
		INSERT OR IGNORE INTO trigger_intents (
			name, description, response_type, answer, is_active, semantic_enabled, created_at, updated_at
		) VALUES (?, ?, 'openai', '', 1, 1, ?, ?)
	""", (
		"нужен VPN",
		"Пользователь прямо или косвенно просит VPN, обход блокировок или способ открыть сервис.",
		now,
		now,
	))
	cursor.execute("""
		INSERT OR IGNORE INTO trigger_intents (
			name, description, response_type, answer, is_active, semantic_enabled, created_at, updated_at
		) VALUES (?, ?, 'openai', '', 1, 1, ?, ?)
	""", (
		"плохо работает связь",
		"Пользователь жалуется на связь, интернет, глушилки, нестабильную сеть или недоступность сервисов.",
		now,
		now,
	))


def _migration_trigger_reply_threads(cursor):
	cursor.execute("""
		CREATE TABLE IF NOT EXISTS trigger_reply_threads (
			id INTEGER PRIMARY KEY AUTOINCREMENT,
			chat_id INTEGER NOT NULL,
			source_user_id INTEGER NOT NULL,
			source_message_id INTEGER NOT NULL,
			bot_account_id INTEGER,
			bot_user_id INTEGER,
			bot_message_id INTEGER NOT NULL,
			trigger_details TEXT,
			followup_sent INTEGER DEFAULT 0,
			created_at TEXT NOT NULL,
			updated_at TEXT NOT NULL,
			UNIQUE(chat_id, bot_message_id)
		)
	""")
	cursor.execute(
		"CREATE INDEX IF NOT EXISTS idx_trigger_reply_threads_lookup "
		"ON trigger_reply_threads(chat_id, bot_message_id, source_user_id, followup_sent)"
	)


def run_migrations():
	migration_handlers = {
		1: _migration_runtime_indexes,
		2: _migration_openai_only_provider,
		3: _migration_account_entity_memberships,
		4: _migration_telegram_api_credentials,
		5: _migration_advanced_triggers,
		6: _migration_trigger_reply_threads,
	}
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("""
			CREATE TABLE IF NOT EXISTS schema_migrations (
				version INTEGER PRIMARY KEY,
				name TEXT NOT NULL,
				applied_at TEXT NOT NULL
			)
		""")
		c.execute("SELECT version FROM schema_migrations")
		applied_versions = {row["version"] for row in c.fetchall()}
		for version, name in DB_MIGRATIONS:
			if version in applied_versions:
				continue
			migration_handlers[version](c)
			c.execute(
				"INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
				(version, name, datetime.now(timezone.utc).isoformat(timespec="seconds"))
			)
			logging.info(f"Applied DB migration {version}: {name}")
		conn.commit()
	except Exception:
		conn.rollback()
		raise
	finally:
		conn.close()


def update_all_tables():
	create_tables()
	_add_column_if_not_exists("groups", "assigned_account_id",
							  "INTEGER DEFAULT NULL REFERENCES accounts(id) ON DELETE SET NULL")
	_add_column_if_not_exists("channels", "assigned_account_id",
							  "INTEGER DEFAULT NULL REFERENCES accounts(id) ON DELETE SET NULL")
	_add_column_if_not_exists("chat_categories", "regular_comment_enabled", "INTEGER DEFAULT 0")
	_add_column_if_not_exists("chat_categories", "regular_comment_interval_minutes", "INTEGER DEFAULT 60")
	_add_column_if_not_exists("chat_categories", "regular_comment_prompt", "TEXT")
	_add_column_if_not_exists("accounts", "is_enabled", "INTEGER DEFAULT 1")
	_add_column_if_not_exists("proxies", "status", "TEXT DEFAULT 'active'")
	_add_column_if_not_exists("proxies", "expires_at", "TEXT")
	_add_column_if_not_exists("proxies", "last_checked_at", "TEXT")
	_add_column_if_not_exists("proxies", "last_error", "TEXT")
	run_migrations()


def record_entity_memberships(account_ids: list[int], entity_type: str, entity_id: int, identifier: str = None):
	from services.subscription_repository import record_entity_memberships as _impl
	return _impl(account_ids, entity_type, entity_id, identifier)


def mark_entity_memberships_left(entity_type: str, entity_ids: list[int], account_ids: list[int] = None):
	from services.subscription_repository import mark_entity_memberships_left as _impl
	return _impl(entity_type, entity_ids, account_ids)


def get_account_subscription_loads() -> dict[int, int]:
	from services.subscription_repository import get_account_subscription_loads as _impl
	return _impl()


def set_config_value(key: str, value: str):
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
	conn.commit()
	conn.close()


def get_config_value(key: str, default_value: str = None) -> str:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("SELECT value FROM config WHERE key=?", (key,))
	row = c.fetchone()
	conn.close()
	if row:
		return row["value"]
	return default_value

def set_template(template_str: str):
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("INSERT OR REPLACE INTO templates (id, templates) VALUES (?, ?)", (1, template_str,))
	conn.commit()
	conn.close()

def get_templates() -> str | None:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("SELECT templates FROM templates WHERE id=1")
	row = c.fetchone()
	conn.close()
	if row:
		return row['templates']
	return None

def add_analytics_log(account_id: int = None, action_type: str = None, details: str = None,
					  success: bool = True):
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute(
			"INSERT INTO analytics_log (account_id, action_type, details, success, timestamp) VALUES (?, ?, ?, ?, ?)",
			(account_id, action_type, details, 1 if success else 0, datetime.now(timezone.utc))
		)
		conn.commit()
	except Exception as e:
		print(f"Error logging analytics: {e}")
	finally:
		conn.close()


def get_analytics_summary(start_date: datetime, end_date: datetime):
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
		SELECT action_type, success, COUNT(*) as count
		FROM analytics_log
		WHERE timestamp BETWEEN ? AND ?
		GROUP BY action_type, success
	""", (start_date, end_date))
	summary = c.fetchall()
	conn.close()
	return summary


def clear_analytics_logs():
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("DELETE FROM analytics_log")
		conn.commit()
		return True
	except Exception as e:
		print(f"Error clearing analytics_log: {e}")
		return False
	finally:
		conn.close()


def update_account_health_status(
		account_id: int,
		status: str,
		details: str = "",
		telegram_user_id: int = None,
		is_authorized: bool = False
):
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("""
			INSERT INTO account_health (
				account_id, status, last_checked_at, details, telegram_user_id, is_authorized
			)
			VALUES (?, ?, ?, ?, ?, ?)
			ON CONFLICT(account_id) DO UPDATE SET
				status = excluded.status,
				last_checked_at = excluded.last_checked_at,
				details = excluded.details,
				telegram_user_id = excluded.telegram_user_id,
				is_authorized = excluded.is_authorized
		""", (
			account_id,
			status,
			datetime.now(timezone.utc).isoformat(timespec="seconds"),
			details,
			telegram_user_id,
			1 if is_authorized else 0
		))
		conn.commit()
	except Exception as e:
		print(f"Error updating account health: {e}")
	finally:
		conn.close()


def get_account_health_summary() -> dict:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("SELECT status, COUNT(*) AS cnt FROM account_health GROUP BY status")
	rows = c.fetchall()
	conn.close()
	summary = {"ok": 0, "warning": 0, "bad": 0, "disabled": 0, "unknown": 0}
	for row in rows:
		status = row["status"] or "unknown"
		summary[status] = row["cnt"]
	return summary


def list_account_health(limit: int = 20) -> list[dict]:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
		SELECT h.*, a.label, a.session_name, a.phone, a.is_enabled
		FROM account_health h
		LEFT JOIN accounts a ON a.id = h.account_id
		ORDER BY h.last_checked_at DESC
		LIMIT ?
	""", (limit,))
	rows = [dict(row) for row in c.fetchall()]
	conn.close()
	return rows


def record_account_runtime_event(
		account_id: int,
		action_type: str,
		success: bool,
		cooldown_until: str = None,
		last_error: str = None
):
	if not account_id or not action_type:
		return
	now = datetime.now(timezone.utc)
	day_key = now.strftime("%Y-%m-%d")
	now_text = now.isoformat(timespec="seconds")
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("""
			SELECT day_key, attempts_today, successes_today, failures_today
			FROM account_runtime_state
			WHERE account_id = ? AND action_type = ?
		""", (account_id, action_type))
		row = c.fetchone()
		if row and row["day_key"] == day_key:
			attempts = int(row["attempts_today"] or 0) + 1
			successes = int(row["successes_today"] or 0) + (1 if success else 0)
			failures = int(row["failures_today"] or 0) + (0 if success else 1)
		else:
			attempts = 1
			successes = 1 if success else 0
			failures = 0 if success else 1

		c.execute("""
			INSERT INTO account_runtime_state (
				account_id, action_type, day_key, attempts_today, successes_today,
				failures_today, last_action_at, last_success_at, last_failure_at,
				cooldown_until, last_error
			)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(account_id, action_type) DO UPDATE SET
				day_key = excluded.day_key,
				attempts_today = excluded.attempts_today,
				successes_today = excluded.successes_today,
				failures_today = excluded.failures_today,
				last_action_at = excluded.last_action_at,
				last_success_at = COALESCE(excluded.last_success_at, account_runtime_state.last_success_at),
				last_failure_at = COALESCE(excluded.last_failure_at, account_runtime_state.last_failure_at),
				cooldown_until = excluded.cooldown_until,
				last_error = excluded.last_error
		""", (
			account_id,
			action_type,
			day_key,
			attempts,
			successes,
			failures,
			now_text,
			now_text if success else None,
			None if success else now_text,
			cooldown_until,
			None if success else (last_error or "")
		))
		conn.commit()
	except Exception as e:
		print(f"Error recording account runtime event: {e}")
	finally:
		conn.close()


def set_account_runtime_cooldown(account_id: int, action_type: str, cooldown_until: str, reason: str = None):
	if not account_id or not action_type:
		return
	conn = get_db_connection()
	c = conn.cursor()
	now = datetime.now(timezone.utc)
	day_key = now.strftime("%Y-%m-%d")
	now_text = now.isoformat(timespec="seconds")
	try:
		c.execute("""
			INSERT INTO account_runtime_state (
				account_id, action_type, day_key, last_action_at, cooldown_until, last_error
			)
			VALUES (?, ?, ?, ?, ?, ?)
			ON CONFLICT(account_id, action_type) DO UPDATE SET
				day_key = excluded.day_key,
				last_action_at = excluded.last_action_at,
				cooldown_until = excluded.cooldown_until,
				last_error = excluded.last_error
		""", (account_id, action_type, day_key, now_text, cooldown_until, reason))
		conn.commit()
	except Exception as e:
		print(f"Error setting account cooldown: {e}")
	finally:
		conn.close()


def list_account_runtime_state(limit: int = 50) -> list[dict]:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
		SELECT s.*, a.label, a.session_name, a.phone
		FROM account_runtime_state s
		LEFT JOIN accounts a ON a.id = s.account_id
		ORDER BY COALESCE(s.last_action_at, '') DESC
		LIMIT ?
	""", (limit,))
	rows = [dict(row) for row in c.fetchall()]
	conn.close()
	return rows


def record_bot_error(level: str, source: str, message: str, details: str = None, account_id: int = None):
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("""
			INSERT INTO bot_error_log (timestamp, level, source, message, details, account_id)
			VALUES (?, ?, ?, ?, ?, ?)
		""", (
			datetime.now(timezone.utc).isoformat(timespec="seconds"),
			level,
			source,
			message,
			details,
			account_id
		))
		conn.commit()
	except Exception:
		pass
	finally:
		conn.close()


def list_bot_errors(limit: int = 50) -> list[dict]:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
		SELECT e.*, a.label, a.session_name
		FROM bot_error_log e
		LEFT JOIN accounts a ON a.id = e.account_id
		ORDER BY e.id DESC
		LIMIT ?
	""", (limit,))
	rows = [dict(row) for row in c.fetchall()]
	conn.close()
	return rows


def clear_bot_errors() -> int:
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("SELECT COUNT(*) AS cnt FROM bot_error_log")
		count = c.fetchone()["cnt"]
		c.execute("DELETE FROM bot_error_log")
		conn.commit()
		return count
	except Exception:
		conn.rollback()
		return 0
	finally:
		conn.close()


def count_bot_errors() -> int:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("SELECT COUNT(*) AS cnt FROM bot_error_log")
	count = c.fetchone()["cnt"]
	conn.close()
	return count


def record_health_check_run(check_type: str, started_at: str, status: str, details: str = None):
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("""
			INSERT INTO health_check_runs (check_type, started_at, finished_at, status, details)
			VALUES (?, ?, ?, ?, ?)
		""", (
			check_type,
			started_at,
			datetime.now(timezone.utc).isoformat(timespec="seconds"),
			status,
			details
		))
		conn.commit()
	except Exception as e:
		print(f"Error recording health check run: {e}")
	finally:
		conn.close()


def list_health_check_runs(limit: int = 10) -> list[dict]:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
		SELECT *
		FROM health_check_runs
		ORDER BY id DESC
		LIMIT ?
	""", (limit,))
	rows = [dict(row) for row in c.fetchall()]
	conn.close()
	return rows


def get_active_category_prompt_for_chat(chat_id: int) -> str | None:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
		SELECT cc.prompt
		FROM category_joined_chats cjc
		JOIN chat_categories cc ON cjc.category_name = cc.category_name
		WHERE cjc.chat_id = ? AND cc.is_active = 1 AND cc.prompt IS NOT NULL AND cc.prompt != ''
		LIMIT 1
	""", (chat_id,))
	row = c.fetchone()
	conn.close()
	if row:
		return row["prompt"]
	return None


def unassign_proxy_for_account(account_id: int):
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("UPDATE proxies SET assigned_account_id = NULL WHERE assigned_account_id = ?", (account_id,))
		conn.commit()
		logging.info(f"Proxy unassigned for account_id: {account_id}")
	except Exception as e:
		logging.error(f"Error unassigning proxy for account {account_id}: {e}")
		conn.rollback()
	finally:
		conn.close()


async def remove_telethon_account(acc_id: int) -> str:
	from userbot import remove_client_from_runtime
	unassign_proxy_for_account(acc_id)

	conn = get_db_connection()
	c = conn.cursor()
	c.execute("SELECT session_name, user_id FROM accounts WHERE id=?", (acc_id,))
	row = c.fetchone()
	if not row:
		conn.close()
		return f"Аккаунт с ID={acc_id} не найден."

	session_name = row["session_name"]

	c.execute("DELETE FROM accounts WHERE id=?", (acc_id,))
	c.execute("DELETE FROM category_joined_chats WHERE account_db_id=?", (acc_id,))
	c.execute("DELETE FROM account_entity_memberships WHERE account_id=?", (acc_id,))
	c.execute("DELETE FROM sessions WHERE session_id=?", (session_name,))
	conn.commit()
	conn.close()

	await remove_client_from_runtime(acc_id, session_name)

	try:
		session_file_path = os.path.join(get_workspace_session_dir(), f"{session_name}.session")
		if os.path.exists(session_file_path):
			os.remove(session_file_path)

		session_journal_path = f"{session_file_path}-journal"
		if os.path.exists(session_journal_path):
			os.remove(session_journal_path)

		return f"Аккаунт (session={session_name}, id={acc_id}) удалён из БД, runtime и файл сессии стерт."
	except Exception as e:
		logging.error(f"Could not remove session file for {session_name}: {e}")
		return f"Аккаунт (session={session_name}, id={acc_id}) удалён из БД и runtime, но не удалось удалить файл сессии."


def get_category_keywords(category_name: str) -> list:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("SELECT keyword, answer, response_type FROM category_keywords WHERE category_name = ?", (category_name,))
	rows = c.fetchall()
	conn.close()
	return [dict(row) for row in rows]


def add_category_keyword(category_name: str, keyword: str, answer: str, response_type: str):
	conn = get_db_connection()
	c = conn.cursor()
	c.execute(
		"INSERT OR REPLACE INTO category_keywords (category_name, keyword, answer, response_type) VALUES (?, ?, ?, ?)",
		(category_name, keyword.lower(), answer, response_type))
	conn.commit()
	conn.close()


def delete_category_keyword(category_name: str, keyword: str):
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("DELETE FROM category_keywords WHERE category_name = ? AND keyword = ?", (category_name, keyword.lower()))
	conn.commit()
	conn.close()


def add_openai_api_key(api_key: str, custom_label: str = None) -> bool:
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("INSERT INTO openai_api_keys (api_key, custom_label, is_active, failure_count) VALUES (?, ?, 1, 0)",
				  (api_key, custom_label if custom_label else None))
		conn.commit()
		return True
	except sqlite3.IntegrityError:
		return False
	finally:
		conn.close()


def get_openai_api_keys() -> list:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute(
		"SELECT id, api_key, is_active, last_used_timestamp, last_failed_timestamp, failure_count, custom_label FROM openai_api_keys ORDER BY id")
	keys = [dict(row) for row in c.fetchall()]
	conn.close()
	return keys


def get_active_openai_api_keys_for_cycle() -> list:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("""
		SELECT id, api_key
		FROM openai_api_keys
		WHERE is_active = 1
		ORDER BY
			last_used_timestamp ASC,
			failure_count ASC,
			last_failed_timestamp ASC
	""")
	keys = [dict(row) for row in c.fetchall()]
	conn.close()
	return keys


def delete_openai_api_key(key_id: int) -> bool:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("DELETE FROM openai_api_keys WHERE id = ?", (key_id,))
	conn.commit()
	deleted_rows = c.rowcount
	conn.close()
	return deleted_rows > 0


def update_openai_api_key_status(key_id: int, is_active: bool) -> bool:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("UPDATE openai_api_keys SET is_active = ? WHERE id = ?", (1 if is_active else 0, key_id))
	conn.commit()
	updated_rows = c.rowcount
	conn.close()
	return updated_rows > 0


def record_openai_key_usage(key_id: int):
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("UPDATE openai_api_keys SET last_used_timestamp = ? WHERE id = ?",
			  (datetime.now(timezone.utc), key_id))
	conn.commit()
	conn.close()


def record_openai_key_failure(key_id: int, temporarily_deactivate: bool = False):
	conn = get_db_connection()
	c = conn.cursor()
	if temporarily_deactivate:
		c.execute("""
			UPDATE openai_api_keys
			SET last_failed_timestamp = ?,
				failure_count = failure_count + 1,
				is_active = 0
			WHERE id = ?
		""", (datetime.now(timezone.utc), key_id))
	else:
		c.execute("""
			UPDATE openai_api_keys
			SET last_failed_timestamp = ?,
				failure_count = failure_count + 1
			WHERE id = ?
		""", (datetime.now(timezone.utc), key_id))
	conn.commit()
	conn.close()


def get_account_details(account_id: int) -> dict | None:
	conn = get_db_connection()
	c = conn.cursor()
	c.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
	row = c.fetchone()
	conn.close()
	return dict(row) if row else None


def create_account_with_proxy(account_data: dict, proxy_id: int | None = None) -> int:
	fields = (
		"session_name", "phone", "api_id", "api_hash", "label", "user_id",
		"proxy_type", "proxy_ip", "proxy_port", "proxy_username", "proxy_password"
	)
	values = [account_data.get(field) for field in fields]
	with db_transaction() as (_, cursor):
		cursor.execute(
			f"INSERT INTO accounts ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
			values
		)
		account_id = cursor.lastrowid
		if proxy_id:
			cursor.execute(
				"""
				UPDATE proxies
				SET assigned_account_id = ?
				WHERE id = ?
				  AND assigned_account_id IS NULL
				  AND COALESCE(status, 'active') = 'active'
				""",
				(account_id, proxy_id)
			)
			if cursor.rowcount != 1:
				raise sqlite3.IntegrityError(f"Proxy {proxy_id} is not available for assignment.")
		return account_id


def update_account_proxy_settings(account_id: int, proxy_details: dict) -> bool:
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute("""
			UPDATE accounts
			SET proxy_type = ?, proxy_ip = ?, proxy_port = ?,
				proxy_username = ?, proxy_password = ?
			WHERE id = ?
		""", (
			proxy_details.get('proxy_type'),
			proxy_details.get('proxy_ip'),
			proxy_details.get('proxy_port'),
			proxy_details.get('proxy_username'),
			proxy_details.get('proxy_password'),
			account_id
		))
		conn.commit()
		return c.rowcount > 0
	except Exception as e:
		logging.error(f"Error updating proxy for account {account_id}: {e}")
		conn.rollback()
		return False
	finally:
		conn.close()


def get_proxy_summary() -> dict:
	from services.proxy_repository import get_proxy_summary as _impl
	return _impl()


def refresh_expired_proxies() -> list[int]:
	from services.proxy_repository import refresh_expired_proxies as _impl
	return _impl()


def update_proxy_check_result(proxy_id: int, status: str, last_error: str = None):
	from services.proxy_repository import update_proxy_check_result as _impl
	return _impl(proxy_id, status, last_error)


def get_all_proxies_for_check() -> list[dict]:
	from services.proxy_repository import get_all_proxies_for_check as _impl
	return _impl()


def get_account_proxy_block_reason(account_data: dict) -> str | None:
	from services.proxy_repository import get_account_proxy_block_reason as _impl
	return _impl(account_data)


def list_proxies(page: int = 1, per_page: int = 8) -> tuple[list[dict], int, int]:
	from services.proxy_repository import list_proxies as _impl
	return _impl(page, per_page)


def get_proxy_details(proxy_id: int) -> dict | None:
	from services.proxy_repository import get_proxy_details as _impl
	return _impl(proxy_id)


def delete_proxy(proxy_id: int) -> bool:
	from services.proxy_repository import delete_proxy as _impl
	return _impl(proxy_id)


def delete_expired_proxies() -> int:
	from services.proxy_repository import delete_expired_proxies as _impl
	return _impl()


def delete_all_proxies() -> int:
	from services.proxy_repository import delete_all_proxies as _impl
	return _impl()


def set_entity_assigned_account(entity_id: int, account_id: int, entity_type: str):
	if entity_type not in ["group", "channel"]:
		logging.error(f"Unknown entity type for assignment: {entity_type}")
		return
	table_name = "groups" if entity_type == "group" else "channels"
	conn = get_db_connection()
	c = conn.cursor()
	try:
		c.execute(f"UPDATE {table_name} SET assigned_account_id = ? WHERE id = ?", (account_id, entity_id))
		conn.commit()
		logging.info(f"Assigned account {account_id} to {entity_type} {entity_id}")
	except Exception as e:
		logging.error(f"Error assigning account to {entity_type} {entity_id}: {e}")
		conn.rollback()
	finally:
		conn.close()


def get_entity_assigned_account_id(entity_id: int, entity_type: str) -> int | None:
	if entity_type not in ["group", "channel"]:
		logging.error(f"Unknown entity type for getting assignment: {entity_type}")
		return None
	table_name = "groups" if entity_type == "group" else "channels"
	conn = get_db_connection()
	c = conn.cursor()
	c.execute(f"SELECT assigned_account_id FROM {table_name} WHERE id = ?", (entity_id,))
	row = c.fetchone()
	conn.close()
	return row['assigned_account_id'] if row and row['assigned_account_id'] is not None else None


def add_proxies_bulk(proxies: list[dict]) -> tuple[int, int]:
	from services.proxy_repository import add_proxies_bulk as _impl
	return _impl(proxies)


def get_unassigned_proxy() -> dict | None:
	from services.proxy_repository import get_unassigned_proxy as _impl
	return _impl()


def assign_proxy_to_account(proxy_id: int, account_id: int):
	from services.proxy_repository import assign_proxy_to_account as _impl
	return _impl(proxy_id, account_id)


def upsert_proxy_for_account(account_id: int, proxy_details: dict):
	from services.proxy_repository import upsert_proxy_for_account as _impl
	return _impl(account_id, proxy_details)
