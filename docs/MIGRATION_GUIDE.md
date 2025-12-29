# Migration Guide

## Workflow
1) Add new migration file under `src/database/migrations/` using incremental numeric prefix, e.g. `002_add_account_health_table.py`.
2) Define:
   - `version`: unique string
   - `description`: human-readable summary
   - `dependencies`: list of required prior migrations
   - `up(conn)`: apply migration (run inside transaction)
   - `down(conn)`: rollback (if reversible)
3) Keep migrations idempotent when possible.

## Running
- Application startup runs `run_migrations(engine)` automatically after creating engine.
- SQLite backups are created before migrations when enabled.
- Registry table `migrations_applied` records applied versions with timestamp, success, duration.

## Safety
- Always back up production DB before applying migrations.
- Keep migrations small and focused; avoid long-running data loops—use batched SQL.
- For schema changes, prefer add-nullable → backfill → enforce constraints.

## Rollback
- Use `down()` if provided; otherwise restore from backup.
- After rollback, verify integrity (`PRAGMA integrity_check` for SQLite).

## Example Template
```python
version = "002_add_field_x"
description = "Add field_x to accounts"
dependencies = ["001_initial_schema"]
is_reversible = True

def up(conn):
    conn.execute(sa.text("ALTER TABLE accounts ADD COLUMN field_x TEXT"))

def down(conn):
    conn.execute(sa.text("ALTER TABLE accounts DROP COLUMN field_x"))
```
