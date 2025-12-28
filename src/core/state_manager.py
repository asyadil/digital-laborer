"""Persistent state management backed by the database."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy.exc import SQLAlchemyError

from src.database.models import SystemState
from src.database.operations import DatabaseSessionManager


@dataclass(frozen=True)
class StateSnapshot:
    key: str
    value: Dict[str, Any]
    updated_at: Optional[datetime]


class StateManager:
    def __init__(self, db: DatabaseSessionManager, logger: Optional[logging.Logger] = None) -> None:
        self.db = db
        self.logger = logger or logging.getLogger("state_manager")

    def get_state(self, key: str) -> Optional[StateSnapshot]:
        try:
            with self.db.session_scope(logger=self.logger) as session:
                row = session.query(SystemState).filter(SystemState.key == key).first()
                if row is None:
                    return None
                return StateSnapshot(key=row.key, value=row.value_json or {}, updated_at=row.updated_at)
        except SQLAlchemyError as exc:
            self.logger.error("State read failed", extra={"component": "state", "key": key, "error": str(exc)})
            return None

    def set_state(self, key: str, value: Dict[str, Any]) -> bool:
        try:
            with self.db.session_scope(logger=self.logger) as session:
                row = session.query(SystemState).filter(SystemState.key == key).first()
                if row is None:
                    row = SystemState(key=key, value_json=value)
                    session.add(row)
                else:
                    row.value_json = value
                return True
        except SQLAlchemyError as exc:
            self.logger.error("State write failed", extra={"component": "state", "key": key, "error": str(exc)})
            return False

    def delete_state(self, key: str) -> bool:
        try:
            with self.db.session_scope(logger=self.logger) as session:
                row = session.query(SystemState).filter(SystemState.key == key).first()
                if row is None:
                    return True
                session.delete(row)
                return True
        except SQLAlchemyError as exc:
            self.logger.error("State delete failed", extra={"component": "state", "key": key, "error": str(exc)})
            return False
