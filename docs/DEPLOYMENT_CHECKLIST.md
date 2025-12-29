# Deployment Checklist

- [ ] Python 3.10+ installed
- [ ] `.env` created with real values (no placeholders)
- [ ] Encryption key backed up (`data/.encryption_key` + off-host)
- [ ] Database reachable; migrations run automatically on start
- [ ] Required directories exist and writable: `data/`, `data/logs/`, `data/backups/`, `data/screenshots/`
- [ ] Disk space >= 1GB free
- [ ] Telegram bot token/chat ID valid (pre-flight passes)
- [ ] External adapters configured or disabled explicitly
- [ ] Logs path writable
- [ ] Health checks pass after startup; no critical errors
- [ ] Backups configured (DB + configs)
