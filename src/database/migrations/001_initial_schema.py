"""Initial schema migration."""
from __future__ import annotations

from sqlalchemy.engine import Connection

from src.database.models import Base

version = "001_initial_schema"
description = "Create core tables for accounts, posts, metrics, state, and logs"
dependencies: list[str] = []
is_reversible = False


def up(conn: Connection) -> None:
    """Create all tables defined in Base metadata."""
    Base.metadata.create_all(conn)


def down(conn: Connection) -> None:
    """No-op: irreversible baseline."""
    pass
