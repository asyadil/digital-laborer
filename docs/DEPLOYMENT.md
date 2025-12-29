# Deployment Guide

## Prerequisites
- Python 3.10+
- SQLite (default) or configure another DB engine in `config/config.yaml`.
- Telegram bot token & chat ID.

## Local Setup
1. Create virtualenv and install requirements:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy sample config and fill secrets:
   ```bash
   cp config/config.sample.yaml config/config.yaml
   ```
3. Run migrations (SQLite auto-creates tables):
   ```bash
   python -m src.core.orchestrator config/config.yaml
   ```

## Systemd Service (Linux)
Create `/etc/systemd/system/referral.service`:
```
[Unit]
Description=Referral Automation System
After=network.target

[Service]
Type=simple
User=www-data
WorkingDirectory=/opt/referral
ExecStart=/usr/bin/python -m src.core.orchestrator config/config.yaml
Restart=on-failure
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```
Then:
```bash
systemctl daemon-reload
systemctl enable referral
systemctl start referral
```

## Docker (basic)
Example `Dockerfile`:
```Dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "-m", "src.core.orchestrator", "config/config.yaml"]
```
Build & run:
```bash
docker build -t referral .
docker run -d --name referral -v $(pwd)/config:/app/config referral
```

## Backups
- SQLite: snapshot the `.db` file periodically; ensure atomic copy (stop service or use `sqlite3 .backup`).
- Logs: rotate via log settings or external logrotate.

## Health & Monitoring
- Health checks run every 5 minutes; Telegram alerts on unhealthy status.
- Daily/weekly analytics sent via Telegram; ensure bot/user chat IDs are valid.

## Checklist before production
- Config validated (see CONFIGURATION.md).
- Telegram bot reachable; test `/start`.
- Accounts loaded in DB with encrypted passwords.
- CAPTCHA/2FA handler tested via Telegram.
- Backups configured for DB and configs.
