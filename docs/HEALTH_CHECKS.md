# Health Checks

## Startup Pre-flight (blocking)
- Environment variables present & non-placeholder.
- Filesystem directories exist & writable: data/, data/logs/, data/backups/, data/screenshots/.
- Disk space warning if <1GB.
- Database reachable; integrity check for SQLite.
- Dependency imports (cryptography, sqlalchemy).
- Telegram token format validation.

## Startup Health Verification (blocking)
- Database: `SELECT 1`, integrity (SQLite), latency check.
- Telegram: send test notification (degraded fallback if fails).
- Scheduler: running flag.
- Critical failures stop startup.

## Runtime Health (scheduler every 5m)
- Database, Telegram, disk, memory, platforms.
- Results logged; non-healthy sets service health to degraded; alerts via Telegram when available.
- Recovery loop retries degraded adapters every 5–10m.

## Actions on Failure
- Critical (database): block startup / exit gracefully.
- Optional (telegram/adapters/monitoring): degrade mode, log, auto-retry.

## Manual Checks
- Run application and inspect console/logs for the formatted pre-flight report.
- Trigger health check via scheduler or Telegram command (when available).
