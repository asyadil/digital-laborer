"""Versioned migration runner with registry tracking."""
from __future__ import annotations

import importlib
import logging
import shutil
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Callable, List, Optional

from sqlalchemy import Table, Column, Integer, String, Boolean, Text, DateTime, MetaData, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from src.database.models import Base

logger = logging.getLogger(__name__)

MIGRATIONS_PACKAGE = "src.database.migrations"
MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def backup_sqlite_database(db_path: str, backup_dir: Optional[str] = None) -> Optional[Path]:
    """Create a timestamped copy of the SQLite database for safety."""
    database_file = Path(db_path)
    if not database_file.exists():
        return None

    target_dir = Path(backup_dir or database_file.parent)
    target_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_file = target_dir / f"{database_file.stem}_backup_{timestamp}{database_file.suffix}"
    shutil.copy2(database_file, backup_file)
    return backup_file


def _ensure_registry(engine: Engine) -> Table:
    """Create migrations_applied registry table if absent."""
    metadata = MetaData()
    table = Table(
        "migrations_applied",
        metadata,
        Column("id", Integer, primary_key=True, autoincrement=True),
        Column("version", String(200), unique=True, nullable=False, index=True),
        Column("description", Text),
        Column("applied_at", DateTime, nullable=False),
        Column("applied_by", String(100)),
        Column("success", Boolean, nullable=False, default=True),
        Column("error_message", Text),
        Column("duration_ms", Integer),
    )
    metadata.create_all(engine, tables=[table])
    return table


def _load_migration_modules() -> List[str]:
    files = sorted(f.stem for f in MIGRATIONS_DIR.glob("*.py") if f.stem not in {"__init__"})
    return files


def _import_migration(module_name: str):
    return importlib.import_module(f"{MIGRATIONS_PACKAGE}.{module_name}")


def run_migrations(engine: Engine, backup_first: bool = True, backup_dir: Optional[str] = None) -> None:
    """Apply pending migrations in order with registry tracking."""
    url = str(engine.url)
    if backup_first and url.startswith("sqlite"):
        db_path = url.split("///")[-1]
        backup_sqlite_database(db_path, backup_dir=backup_dir)

    # Ensure base metadata exists for initial schema
    Base.metadata.create_all(engine)

    registry = _ensure_registry(engine)
    applied_versions = set()
    with engine.connect() as conn:
        existing = conn.execute(select(registry.c.version)).fetchall()
        applied_versions = {row[0] for row in existing}

    migrations = _load_migration_modules()
    for module_name in migrations:
        module = _import_migration(module_name)
        version = getattr(module, "version", module_name)
        description = getattr(module, "description", "")
        up_fn: Callable[[Engine], None] = getattr(module, "up", None)
        if not up_fn:
            logger.warning("Migration %s missing up() function; skipping", module_name)
            continue
        if version in applied_versions:
            continue

        start = time.perf_counter()
        success = True
        error_message = None
        try:
            with engine.begin() as conn:
                up_fn(conn)
        except SQLAlchemyError as exc:
            success = False
            error_message = str(exc)
            logger.error("Migration %s failed: %s", version, exc)
            raise
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            with engine.begin() as conn:
                conn.execute(
                    registry.insert().values(
                        version=version,
                        description=description,
                        applied_at=datetime.now(UTC),
                        applied_by="system",
                        success=success,
                        error_message=error_message,
                        duration_ms=duration_ms,
                    )
                )
        logger.info("Applied migration %s in %sms", version, duration_ms)
