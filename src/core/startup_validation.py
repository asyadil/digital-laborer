"""Pre-flight validation before starting services."""
from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from sqlalchemy import text, inspect
from sqlalchemy.engine import Engine

PLACEHOLDERS = {"", "REPLACE_ME", "CHANGE_ME", "TODO", "xxx", "XXXX", "REDACTED"}


@dataclass
class CheckResult:
    name: str
    status: str  # OK, WARNING, ERROR
    messages: List[str]

    def is_error(self) -> bool:
        return self.status.upper() == "ERROR"

    def is_warning(self) -> bool:
        return self.status.upper() == "WARNING"


@dataclass
class PreflightReport:
    results: List[CheckResult]

    @property
    def errors(self) -> List[CheckResult]:
        return [r for r in self.results if r.is_error()]

    @property
    def warnings(self) -> List[CheckResult]:
        return [r for r in self.results if r.is_warning()]

    def format(self) -> str:
        lines = ["🔍 SYSTEM PRE-FLIGHT CHECKS", "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
        for r in self.results:
            icon = {"OK": "✓", "WARNING": "⚠", "ERROR": "✗"}.get(r.status.upper(), "?")
            lines.append(f"[{icon}] {r.name.ljust(28)} {r.status.upper()}")
            for msg in r.messages:
                lines.append(f"    → {msg}")
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        if self.errors:
            lines.append(f"❌ STARTUP BLOCKED: {len(self.errors)} critical error(s) found")
        else:
            warn_count = len(self.warnings)
            status_line = "✅ ALL CHECKS PASSED" if warn_count == 0 else f"⚠ STARTUP WITH {warn_count} WARNING(S)"
            lines.append(status_line)
        return "\n".join(lines)


def _is_placeholder(value: Optional[str]) -> bool:
    if value is None:
        return True
    cleaned = value.strip()
    if not cleaned:
        return True
    lowered = cleaned.lower()
    return cleaned in PLACEHOLDERS or lowered in {"changeme", "replace_me", "xxx"}


def _check_env(required: Iterable[str]) -> CheckResult:
    missing: List[str] = []
    placeholders: List[str] = []
    for key in required:
        val = os.getenv(key)
        if val is None:
            missing.append(key)
        elif _is_placeholder(val):
            placeholders.append(key)
    msgs: List[str] = []
    status = "OK"
    if missing:
        status = "ERROR"
        msgs.append(f"Missing: {', '.join(missing)}")
    if placeholders:
        status = "ERROR"
        msgs.append(f"Placeholder values: {', '.join(placeholders)}")
    if not msgs:
        msgs.append("All required environment variables present")
    return CheckResult("Environment Variables", status, msgs)


def _check_filesystem(directories: Iterable[Path]) -> CheckResult:
    missing: List[str] = []
    unwritable: List[str] = []
    for d in directories:
        try:
            d.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=d, delete=True):
                pass
        except Exception:
            unwritable.append(str(d))
        if not d.exists():
            missing.append(str(d))
    status = "OK"
    msgs: List[str] = []
    if missing:
        status = "ERROR"
        msgs.append(f"Missing directories: {', '.join(missing)}")
    if unwritable:
        status = "ERROR"
        msgs.append(f"No write permission: {', '.join(unwritable)}")
    if not msgs:
        msgs.append("Filesystem ready")
    return CheckResult("Filesystem Access", status, msgs)


def _check_disk_space(path: Path, minimum_bytes: int = 1_000_000_000) -> CheckResult:
    usage = shutil.disk_usage(path)
    free = usage.free
    msgs = [f"Free: {free/1024/1024:.0f} MB"]
    status = "OK"
    if free < minimum_bytes:
        status = "WARNING" if free >= minimum_bytes * 0.5 else "ERROR"
        msgs.append(f"Low disk space (min recommended {minimum_bytes/1024/1024:.0f} MB)")
    return CheckResult("Disk Space", status, msgs)


def _check_database(engine: Engine, expected_tables: Optional[Iterable[str]] = None) -> CheckResult:
    msgs: List[str] = []
    status = "OK"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            insp = inspect(engine)
            tables = set(insp.get_table_names())
            if expected_tables:
                missing = [t for t in expected_tables if t not in tables]
                if missing:
                    status = "ERROR"
                    msgs.append(f"Missing tables: {', '.join(missing)}")
            if engine.url.get_backend_name().startswith("sqlite"):
                result = conn.execute(text("PRAGMA integrity_check")).scalar()
                if result and result != "ok":
                    status = "ERROR"
                    msgs.append(f"SQLite integrity_check failed: {result}")
            # simple rw test
            conn.execute(text("SELECT datetime('now')"))
    except Exception as exc:
        status = "ERROR"
        msgs.append(f"DB error: {exc}")
    if not msgs:
        msgs.append("Database reachable")
    return CheckResult("Database Connection", status, msgs)


def _check_dependencies(required_modules: Iterable[str]) -> CheckResult:
    msgs: List[str] = []
    status = "OK"
    if sys.version_info < (3, 10):
        status = "ERROR"
        msgs.append("Python 3.10+ required")
    for mod in required_modules:
        try:
            __import__(mod)
        except Exception as exc:
            status = "ERROR"
            msgs.append(f"Missing module: {mod} ({exc})")
    if not msgs:
        msgs.append("Runtime dependencies available")
    return CheckResult("Dependencies", status, msgs)


def _check_tokens(config) -> CheckResult:
    msgs: List[str] = []
    status = "OK"
    token = getattr(config.telegram, "bot_token", "") if hasattr(config, "telegram") else ""
    chat_id = getattr(config.telegram, "user_chat_id", "") if hasattr(config, "telegram") else ""
    if _is_placeholder(token) or not token:
        status = "ERROR"
        msgs.append("Telegram bot token missing/placeholder")
    if _is_placeholder(chat_id) or not chat_id:
        status = "ERROR"
        msgs.append("Telegram user chat id missing/placeholder")
    token_regex = re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")
    if token and not token_regex.match(token):
        status = "ERROR"
        msgs.append("Telegram token format invalid")
    if not msgs:
        msgs.append("Telegram credentials format OK")
    return CheckResult("Telegram Credentials", status, msgs)


def run_preflight_checks(config, engine: Engine, base_path: Path = Path(".")) -> PreflightReport:
    """Run all pre-flight checks. Raises no exceptions; callers decide behavior."""
    required_env = [
        "ENCRYPTION_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_USER_CHAT_ID",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USERNAME",
        "REDDIT_PASSWORD",
        "REDDIT_USER_AGENT",
    ]
    dirs = [
        base_path / "data",
        base_path / "data" / "logs",
        base_path / "data" / "backups",
        base_path / "data" / "screenshots",
    ]
    expected_tables = ["accounts", "posts", "system_metrics"]  # minimal critical tables
    results = [
        _check_env(required_env),
        _check_filesystem(dirs),
        _check_disk_space(base_path),
        _check_database(engine, expected_tables=expected_tables),
        _check_dependencies(["cryptography", "sqlalchemy"]),
        _check_tokens(config),
    ]
    return PreflightReport(results=results)
