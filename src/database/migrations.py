"""Database migration utilities using SQLAlchemy metadata.

This module provides a lightweight migration runner that can be used in
environments without a full Alembic setup. It ensures the current metadata is
applied to the configured database and offers backup hooks for safety.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy.engine import Engine

from src.database.models import Base


def backup_sqlite_database(db_path: str, backup_dir: Optional[str] = None) -> Optional[Path]:
    """Create a timestamped copy of the SQLite database for safety."""
    database_file = Path(db_path)
    if not database_file.exists():
        return None

    target_dir = Path(backup_dir or database_file.parent)
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    backup_file = target_dir / f"{database_file.stem}_backup_{timestamp}{database_file.suffix}"
    shutil.copy2(database_file, backup_file)
    return backup_file


def run_migrations(engine: Engine, backup_first: bool = True, backup_dir: Optional[str] = None) -> None:
    """Apply metadata to the database, optionally taking a backup for SQLite."""
    url = str(engine.url)
    if backup_first and url.startswith("sqlite"):
        db_path = url.split("///")[-1]
        backup_sqlite_database(db_path, backup_dir=backup_dir)
    Base.metadata.create_all(engine)
