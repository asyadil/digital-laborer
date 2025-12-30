"""Lightweight audit logger for Telegram commands (/secret, /netid, etc.)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class AuditLogger:
    _logger = logging.getLogger("audit")
    _file_path: Optional[Path] = None

    @classmethod
    def configure(cls, file_path: Optional[str] = None, logger: Optional[logging.Logger] = None) -> None:
        cls._file_path = Path(file_path) if file_path else None
        if logger:
            cls._logger = logger

    @classmethod
    def log(cls, actor: str, action: str, target: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "action": action,
            "target": target,
            "metadata": metadata or {},
        }
        try:
            cls._logger.info("[AUDIT] %s", json.dumps(entry, ensure_ascii=False))
        except Exception:
            pass
        if cls._file_path:
            try:
                cls._file_path.parent.mkdir(parents=True, exist_ok=True)
                with cls._file_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except Exception:
                pass
