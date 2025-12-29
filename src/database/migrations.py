"""Database migration utilities using SQLAlchemy metadata.

Lightweight migration runner for environments without Alembic.
- Backs up SQLite DB (optional)
- Applies metadata create_all
- Ensures critical columns exist for compatibility with new schema
"""
from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from src.database.models import Base

logger = logging.getLogger(__name__)


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


def _sqlite_table_columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info('{table}')")).fetchall()
    return {row[1] for row in rows}


def _sqlite_add_column_if_missing(
    engine: Engine,
    table: str,
    column: str,
    ddl: str,
    existing: Optional[Iterable[str]] = None,
) -> None:
    existing_cols = set(existing) if existing is not None else _sqlite_table_columns(engine, table)
    if column in existing_cols:
        return
    with engine.connect() as conn:
        logger.info("Adding column %s.%s via ALTER TABLE", table, column)
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))


def _sqlite_patch_posts(engine: Engine) -> None:
    """Ensure posts table has new columns introduced in schema updates."""
    cols = _sqlite_table_columns(engine, "posts")
    # external_id: TEXT
    _sqlite_add_column_if_missing(engine, "posts", "external_id", "TEXT", existing=cols)
    cols.add("external_id")
    # error_message: TEXT
    _sqlite_add_column_if_missing(engine, "posts", "error_message", "TEXT", existing=cols)
    cols.add("error_message")
    # quality_breakdown: store as TEXT (JSON serialized)
    _sqlite_add_column_if_missing(engine, "posts", "quality_breakdown", "TEXT", existing=cols)


def run_migrations(engine: Engine, backup_first: bool = True, backup_dir: Optional[str] = None) -> None:
    """Apply metadata and patch critical columns if schema evolved."""
    url = str(engine.url)
    if backup_first and url.startswith("sqlite"):
        db_path = url.split("///")[-1]
        backup_sqlite_database(db_path, backup_dir=backup_dir)

    # Base metadata (idempotent create_all)
    Base.metadata.create_all(engine)

    # Lightweight column patching for SQLite
    if url.startswith("sqlite"):
        _sqlite_patch_posts(engine)
