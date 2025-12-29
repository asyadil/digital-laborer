# Disaster Recovery Guide

## Encryption Key Loss
1) Locate backup `data/.encryption_key` or off-host copy.
2) If missing, restore from secure backup; without the key encrypted data cannot be decrypted.
3) Set `ENCRYPTION_KEY` in environment and in `.env` (600 perms) to match the backup key.
4) Restart application; verify decryption of existing credentials.

## Database Corruption (SQLite)
1) Stop services.
2) Restore latest backup from `data/database_backup_*.db` (created pre-migration).
3) Run migrations with the restored DB: start app normally (migrations run automatically).
4) Validate integrity: `PRAGMA integrity_check` should return `ok`.

## Failed Migration
1) Restore pre-migration backup (see backup file timestamp).
2) Fix migration script; rerun application to apply pending migrations.
3) Verify application starts without registry errors.

## Telegram/Adapter Outage
- System will run in degraded mode; notifications logged locally.
- Connectivity auto-retries every 5 minutes; no manual action needed unless credentials invalid.

## Key Points
- Keep encryption key backed up securely off-host.
- Keep regular database backups.
- Never commit secrets to version control.
