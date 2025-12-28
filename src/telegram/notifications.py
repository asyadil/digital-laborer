"""Telegram notification helpers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class Notification:
    message: str
    priority: str = "INFO"
    attachments: Optional[list[str]] = None
    reply_to_message_id: Optional[int] = None
    parse_mode: Optional[str] = "MarkdownV2"
    metadata: Optional[dict[str, Any]] = None
