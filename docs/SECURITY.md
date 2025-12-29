# Security Best Practices

## Key Management
- Encryption key persisted in `.env` and `data/.encryption_key` with 600 perms (POSIX).
- Never log key material; rotate via regenerating key and re-encrypt secrets.
- Ensure `.env` and key files remain gitignored.

## Secrets Handling
- Load secrets from environment first, then secret manager, then `.env` fallback.
- Reject placeholder values (`REPLACE_ME`, `xxx`, empty).
- Do not echo secrets in logs or error messages.

## Access Control
- Restrict Telegram bot to configured `TELEGRAM_USER_CHAT_ID`.
- Lock down data/ directory permissions (700 recommended).

## Network & Dependencies
- Require Python 3.10+ and vetted dependencies from `requirements.txt`.
- Validate external tokens before startup (pre-flight checks).

## Monitoring & Alerts
- Pre-flight and startup health checks block unsafe startup.
- Health monitor logs degraded components and retries recovery.

## Incident Response
- Back up encryption key securely off-host.
- Use migration backups before applying DB changes (SQLite copy).
- Document recovery steps in `DISASTER_RECOVERY.md`.
