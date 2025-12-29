"""Versioned migration definitions."""

from src.database.migration_runner import run_migrations  # re-export for convenience

__all__ = ["run_migrations"]
