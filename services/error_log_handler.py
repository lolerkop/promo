import logging

from db import record_bot_error


class DatabaseErrorLogHandler(logging.Handler):
	def __init__(self):
		super().__init__(level=logging.ERROR)

	def emit(self, record: logging.LogRecord):
		try:
			if record.name.startswith("apscheduler."):
				return
			message = record.getMessage()
			details = None
			if record.exc_info:
				details = self.format(record)
			record_bot_error(
				level=record.levelname,
				source=record.name,
				message=message[:1000],
				details=details[:4000] if details else None,
				account_id=getattr(record, "account_id", None)
			)
		except Exception:
			pass


def install_error_log_handler():
	root = logging.getLogger()
	for handler in root.handlers:
		if isinstance(handler, DatabaseErrorLogHandler):
			return
	db_handler = DatabaseErrorLogHandler()
	db_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
	root.addHandler(db_handler)

