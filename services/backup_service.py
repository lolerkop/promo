import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone

from db import (WORKSPACES_INDEX_PATH, get_current_db_path,
                get_current_workspace_id, get_workspace_session_dir)


BACKUP_DIR = os.getenv("BACKUP_DIR", os.path.join("data", "backups"))


def _add_file_if_exists(zipf: zipfile.ZipFile, file_path: str, arcname: str):
	if file_path and os.path.exists(file_path) and os.path.isfile(file_path):
		zipf.write(file_path, arcname=arcname)


def _add_dir_if_exists(zipf: zipfile.ZipFile, dir_path: str, arc_prefix: str):
	if not dir_path or not os.path.exists(dir_path):
		return
	for root, _, files in os.walk(dir_path):
		for filename in files:
			file_path = os.path.join(root, filename)
			rel_path = os.path.relpath(file_path, dir_path)
			zipf.write(file_path, arcname=os.path.join(arc_prefix, rel_path))


def create_backup(include_sessions: bool = False) -> dict:
	os.makedirs(BACKUP_DIR, exist_ok=True)
	workspace_id = get_current_workspace_id()
	timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
	suffix = "with_sessions" if include_sessions else "db"
	filename = f"promo_backup_{workspace_id}_{timestamp}_{suffix}.zip"
	backup_path = os.path.join(BACKUP_DIR, filename)

	db_path = get_current_db_path()
	reports_dir = os.path.join("data", "reports", workspace_id)

	with tempfile.TemporaryDirectory() as tmp_dir:
		db_copy_path = os.path.join(tmp_dir, "database.sqlite")
		if os.path.exists(db_path):
			source = sqlite3.connect(db_path)
			target = sqlite3.connect(db_copy_path)
			try:
				source.backup(target)
			finally:
				target.close()
				source.close()

		with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
			_add_file_if_exists(zipf, db_copy_path, os.path.join("workspace", "database.sqlite"))
			_add_file_if_exists(zipf, WORKSPACES_INDEX_PATH, os.path.join("data", "workspaces", "workspaces.json"))
			_add_dir_if_exists(zipf, reports_dir, os.path.join("data", "reports", workspace_id))
			if include_sessions:
				_add_dir_if_exists(zipf, get_workspace_session_dir(), os.path.join("sessions", workspace_id))

	size_bytes = os.path.getsize(backup_path)
	return {
		"path": backup_path,
		"filename": filename,
		"size_bytes": size_bytes,
		"include_sessions": include_sessions,
		"workspace_id": workspace_id,
	}


def list_backups(limit: int = 10) -> list[dict]:
	os.makedirs(BACKUP_DIR, exist_ok=True)
	items = []
	for filename in os.listdir(BACKUP_DIR):
		if not filename.endswith(".zip"):
			continue
		path = os.path.join(BACKUP_DIR, filename)
		if not os.path.isfile(path):
			continue
		items.append({
			"filename": filename,
			"path": path,
			"size_bytes": os.path.getsize(path),
			"mtime": os.path.getmtime(path),
		})
	items.sort(key=lambda item: item["mtime"], reverse=True)
	return items[:limit]


def get_latest_backup() -> dict | None:
	backups = list_backups(1)
	return backups[0] if backups else None


def restore_backup(backup_path: str, restore_sessions: bool = False) -> dict:
	if not os.path.exists(backup_path):
		raise FileNotFoundError("Backup file not found.")

	pre_restore = create_backup(include_sessions=False)
	workspace_id = get_current_workspace_id()
	db_path = get_current_db_path()
	db_parent = os.path.dirname(db_path)
	if db_parent:
		os.makedirs(db_parent, exist_ok=True)

	with tempfile.TemporaryDirectory() as tmp_dir:
		with zipfile.ZipFile(backup_path, "r") as zipf:
			names = set(zipf.namelist())
			if "workspace/database.sqlite" not in names:
				raise ValueError("Backup does not contain workspace/database.sqlite.")
			zipf.extractall(tmp_dir)

		extracted_db = os.path.join(tmp_dir, "workspace", "database.sqlite")
		shutil.copy2(extracted_db, db_path)

		extracted_reports = os.path.join(tmp_dir, "data", "reports", workspace_id)
		if os.path.isdir(extracted_reports):
			target_reports = os.path.join("data", "reports", workspace_id)
			os.makedirs(os.path.dirname(target_reports), exist_ok=True)
			if os.path.isdir(target_reports):
				shutil.rmtree(target_reports)
			shutil.copytree(extracted_reports, target_reports)

		extracted_sessions = os.path.join(tmp_dir, "sessions", workspace_id)
		if restore_sessions and os.path.isdir(extracted_sessions):
			target_sessions = get_workspace_session_dir()
			if os.path.isdir(target_sessions):
				shutil.rmtree(target_sessions)
			shutil.copytree(extracted_sessions, target_sessions)

	return {"pre_restore_backup": pre_restore, "restored_from": backup_path}


def human_size(size_bytes: int) -> str:
	size = float(size_bytes or 0)
	for unit in ("B", "KB", "MB", "GB"):
		if size < 1024 or unit == "GB":
			return f"{size:.1f} {unit}"
		size /= 1024
	return f"{size:.1f} GB"
