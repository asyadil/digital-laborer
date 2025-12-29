from pathlib import Path

from sqlalchemy import create_engine, inspect

from src.database.migrations import run_migrations


def test_migrations_registry_created(tmp_path):
    db_path = tmp_path / "db.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")

    run_migrations(engine, backup_first=False)

    insp = inspect(engine)
    tables = insp.get_table_names()
    assert "migrations_applied" in tables
