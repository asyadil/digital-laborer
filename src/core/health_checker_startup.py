"""Post-startup health verification for critical services."""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.engine import Engine


class StartupHealthError(RuntimeError):
    """Raised when a critical health check fails at startup."""


async def _check_database(engine: Engine, timeout: float = 2.0) -> Tuple[str, List[str]]:
    messages: List[str] = []
    status = "healthy"
    try:
        start = time.perf_counter()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            if engine.url.get_backend_name().startswith("sqlite"):
                integrity = conn.execute(text("PRAGMA integrity_check")).scalar()
                if integrity and integrity != "ok":
                    status = "unhealthy"
                    messages.append(f"Integrity check failed: {integrity}")
            elapsed_ms = (time.perf_counter() - start) * 1000
            if elapsed_ms > 250:
                status = "degraded"
                messages.append(f"Slow DB response: {elapsed_ms:.1f} ms")
    except Exception as exc:
        status = "unhealthy"
        messages.append(f"DB error: {exc}")
    if not messages:
        messages.append("DB reachable")
    return status, messages


async def _check_telegram(telegram: Any, timeout: float = 3.0) -> Tuple[str, List[str]]:
    if telegram is None:
        return "degraded", ["Telegram not configured"]
    if hasattr(telegram, "bot_token") and not telegram.bot_token:
        return "unhealthy", ["Telegram bot token missing"]
    try:
        send = telegram.send_notification("✅ Telegram connectivity test", priority="INFO")
        await asyncio.wait_for(send, timeout=timeout)
        return "healthy", ["Telegram notification sent"]
    except Exception as exc:
        return "degraded", [f"Telegram send failed: {exc}"]


async def _check_scheduler(scheduler: Any) -> Tuple[str, List[str]]:
    try:
        # Lightweight check: ensure scheduler loop task not stopped
        if hasattr(scheduler, "is_running") and callable(getattr(scheduler, "is_running")):
            running = scheduler.is_running()
            return ("healthy" if running else "degraded"), [f"Scheduler running={running}"]
    except Exception as exc:
        return "degraded", [f"Scheduler check failed: {exc}"]
    return "healthy", ["Scheduler ready"]


async def run_startup_health_checks(
    engine: Engine,
    telegram: Any,
    scheduler: Any,
    logger: Any,
    critical: Optional[List[str]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Run startup health checks and return structured results.

    Critical components failing will raise StartupHealthError.
    """
    critical = critical or ["database"]
    results: Dict[str, Dict[str, Any]] = {}

    db_status, db_messages = await _check_database(engine)
    results["database"] = {"status": db_status, "messages": db_messages}

    tg_status, tg_messages = await _check_telegram(telegram)
    results["telegram"] = {"status": tg_status, "messages": tg_messages}

    sched_status, sched_messages = await _check_scheduler(scheduler)
    results["scheduler"] = {"status": sched_status, "messages": sched_messages}

    for name, data in results.items():
        logger.info(
            "Startup health",
            extra={"component": "startup_health", "service": name, "status": data["status"], "messages": data["messages"]},
        )

    failures = [n for n in critical if results.get(n, {}).get("status") != "healthy"]
    if failures:
        raise StartupHealthError(f"Critical startup health checks failed: {', '.join(failures)}")
    return results
