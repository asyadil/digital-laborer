"""Structured logging utilities with file rotation and optional Telegram forwarding."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import traceback
from datetime import datetime
from typing import Any, Optional


class TelegramLogHandler(logging.Handler):
    """Forward log records to a Telegram controller that exposes send_notification."""

    def __init__(self, telegram_controller: Any, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.telegram_controller = telegram_controller

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            priority = record.levelname if hasattr(record, "levelname") else "INFO"
            self.telegram_controller.send_notification(message, priority=priority)
        except Exception:
            # Avoid recursive logging errors
            self.handleError(record)


def _ensure_log_directory(log_file: str) -> None:
    directory = os.path.dirname(log_file)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def _default_formatter(fmt: str) -> logging.Formatter:
    return logging.Formatter(fmt)


class RedactionFilter(logging.Filter):
    """Redact common secret patterns from log messages."""

    # Simple patterns for tokens/keys/passwords; expand as needed
    PATTERNS = [
        re.compile(r"(token|password|secret|key)=([A-Za-z0-9_\-\.\/\+:]{8,})", re.IGNORECASE),
        re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.\/\+:]{8,}", re.IGNORECASE),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        redacted = msg
        for pattern in self.PATTERNS:
            redacted = pattern.sub(r"\1=***REDACTED***", redacted)
        # Update the message in-place for handlers/formatters
        if redacted != msg:
            record.msg = redacted
        # Also redact in extra dict if present
        if hasattr(record, "__dict__"):
            for key, value in list(record.__dict__.items()):
                if isinstance(value, str):
                    new_val = value
                    for pattern in self.PATTERNS:
                        new_val = pattern.sub(r"\1=***REDACTED***", new_val)
                    record.__dict__[key] = new_val
        return True


def _json_formatter(record: logging.LogRecord) -> str:
    payload = {
        "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + "Z",
        "level": record.levelname,
        "logger": record.name,
        "message": record.getMessage(),
        "pathname": record.pathname,
        "lineno": record.lineno,
        "component": getattr(record, "component", None),
        "extra": {k: v for k, v in record.__dict__.items() if k not in logging.LogRecord.__dict__},
    }
    if record.exc_info:
        payload["exception"] = "".join(traceback.format_exception(*record.exc_info))
    return json.dumps(payload, ensure_ascii=False)


class JsonFormatter(logging.Formatter):
    """Formatter that outputs JSON for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        return _json_formatter(record)


def setup_logger(
    name: str,
    level: str = "INFO",
    log_file: Optional[str] = None,
    log_format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    max_file_size_mb: int = 10,
    backup_count: int = 5,
    telegram_controller: Any = None,
    json_logs: bool = False,
) -> logging.Logger:
    """Configure and return a logger with rotation and optional Telegram forwarding."""

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    redaction_filter = RedactionFilter()

    formatter: logging.Formatter = JsonFormatter() if json_logs else _default_formatter(log_format)

    if log_file:
        try:
            _ensure_log_directory(log_file)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=max_file_size_mb * 1024 * 1024,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
            file_handler.setFormatter(formatter)
            file_handler.addFilter(redaction_filter)
            logger.addHandler(file_handler)
        except OSError as exc:
            # Fall back to console logging if file handler fails
            fallback = logging.StreamHandler()
            fallback.setLevel(getattr(logging, level.upper(), logging.INFO))
            fallback.setFormatter(formatter)
            fallback.addFilter(redaction_filter)
            logger.addHandler(fallback)
            logger.error(
                "Failed to attach file handler for logging",
                extra={"component": "logger", "error": str(exc)},
            )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redaction_filter)
    logger.addHandler(console_handler)

    if telegram_controller is not None:
        telegram_handler = TelegramLogHandler(telegram_controller=telegram_controller)
        telegram_handler.setFormatter(formatter)
        telegram_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
        telegram_handler.addFilter(redaction_filter)
        logger.addHandler(telegram_handler)

    logger.debug("Logger initialized", extra={"component": "logger", "json_logs": json_logs})
    return logger


def get_child_logger(parent: logging.Logger, child_name: str) -> logging.Logger:
    """Create a child logger with the same handlers and level."""
    child = parent.getChild(child_name)
    child.setLevel(parent.level)
    return child
