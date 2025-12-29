# Production Checklist

- [ ] Configuration validated (`config/config.yaml`): database path, Telegram bot token & chat ID, platform credentials.
- [ ] Secrets stored securely (not in VCS); encrypted passwords for DB-backed accounts.
- [ ] Telegram bot reachable; `/start` tested; human review/CAPTCHA prompts verified.
- [ ] Accounts loaded and healthy: run health check; resolve flagged/unhealthy accounts.
- [ ] CAPTCHA/2FA handler tested on at least one platform.
- [ ] Scheduler running (daily routine, health checks, analytics); time settings correct (UTC).
- [ ] Logging path writable; log rotation configured.
- [ ] Backups configured for SQLite DB and config files.
- [ ] Deployment service set up (systemd or Docker) with restart on failure.
- [ ] Alerts wired: Telegram notifications received for test alert.
- [ ] Analytics report delivered (daily/weekly) and reviewed for correctness.
- [ ] Encryption key backed up; .env and data/.encryption_key present with 600 perms (POSIX).
- [ ] Migrations registry present; migrations applied cleanly; backup taken before migrations.
- [ ] Disk space > 1GB free; data/ directories writable.
