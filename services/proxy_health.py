import asyncio
import time

from db import get_all_proxies_for_check, update_proxy_check_result


def format_proxy_addr(proxy: dict) -> str:
	return f"{proxy.get('proxy_type') or 'socks5'}://{proxy.get('proxy_ip')}:{proxy.get('proxy_port')}"


def progress_bar(done: int, total: int, width: int = 16) -> str:
	if total <= 0:
		return "[" + "-" * width + "]"
	filled = int(width * min(done, total) / total)
	return "[" + "#" * filled + "-" * (width - filled) + "]"


def proxy_display_status(status: str) -> str:
	status = (status or "active").lower()
	if status == "expired":
		return "истек"
	if status == "failed":
		return "не работает"
	if status == "disabled":
		return "выключен"
	return "активный"


def _proxy_type_to_pysocks(proxy_type: str):
	import socks

	normalized = (proxy_type or "socks5").lower()
	if normalized == "socks5":
		return socks.SOCKS5
	if normalized == "socks4":
		return socks.SOCKS4
	if normalized == "http":
		return socks.HTTP
	raise ValueError(f"Unsupported proxy type: {proxy_type}")


def _check_proxy_connectivity_sync(proxy: dict, timeout: int = 12) -> tuple[bool, int | None, str | None]:
	import socks

	targets = (
		("149.154.167.50", 443),
		("91.108.56.100", 443),
	)
	last_error = None
	for host, port in targets:
		sock = socks.socksocket()
		started_at = time.monotonic()
		try:
			sock.set_proxy(
				proxy_type=_proxy_type_to_pysocks(proxy.get("proxy_type")),
				addr=proxy.get("proxy_ip"),
				port=int(proxy.get("proxy_port")),
				username=proxy.get("proxy_username") or None,
				password=proxy.get("proxy_password") or None,
			)
			sock.settimeout(timeout)
			sock.connect((host, port))
			latency_ms = int((time.monotonic() - started_at) * 1000)
			return True, latency_ms, None
		except Exception as e:
			last_error = f"{type(e).__name__}: {str(e)[:220]}"
		finally:
			try:
				sock.close()
			except Exception:
				pass
	return False, None, last_error or "Proxy connection failed."


async def run_proxy_health_check(concurrency: int = 5, progress_callback=None) -> dict:
	proxies = get_all_proxies_for_check()
	total = len(proxies)
	stats = {"active": 0, "failed": 0, "expired": 0}
	affected_accounts = set()
	result_lines = []
	completed = 0
	lock = asyncio.Lock()

	async def notify():
		if progress_callback:
			await progress_callback({
				"total": total,
				"completed": completed,
				"stats": dict(stats),
				"affected_accounts": set(affected_accounts),
				"result_lines": list(result_lines),
			})

	async def check_one(proxy: dict):
		nonlocal completed
		proxy_id = int(proxy["id"])
		proxy_addr = format_proxy_addr(proxy)
		computed_status = (proxy.get("computed_status") or proxy.get("status") or "active").lower()

		if computed_status == "expired":
			result_status = "expired"
			result_text = "истек по дате"
			error_text = "Rental expiration time has passed."
		else:
			ok, latency_ms, error = await asyncio.to_thread(_check_proxy_connectivity_sync, proxy)
			if ok:
				result_status = "active"
				result_text = f"OK {latency_ms} ms"
				error_text = None
			else:
				result_status = "failed"
				result_text = error or "не удалось подключиться"
				error_text = result_text

		async with lock:
			update_proxy_check_result(proxy_id, result_status, error_text[:500] if error_text else None)
			completed += 1
			stats[result_status] += 1
			if result_status in {"failed", "expired"} and proxy.get("assigned_account_id"):
				affected_accounts.add(int(proxy["assigned_account_id"]))
			result_lines.append({
				"id": proxy_id,
				"addr": proxy_addr,
				"status": result_status,
				"text": str(result_text),
			})
			await notify()

	await notify()
	semaphore = asyncio.Semaphore(max(1, int(concurrency or 1)))

	async def limited_check(proxy: dict):
		async with semaphore:
			await check_one(proxy)

	await asyncio.gather(*(limited_check(proxy) for proxy in proxies))
	return {
		"total": total,
		"stats": stats,
		"affected_accounts": sorted(affected_accounts),
		"result_lines": result_lines,
	}

