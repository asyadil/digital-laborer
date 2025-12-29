# Referral Automation System

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Tests](https://img.shields.io/badge/tests-pytest-blue)](#testing-guide)
[![Coverage](https://img.shields.io/badge/coverage-75%25%2B-brightgreen)](#testing-guide)

> Production-grade, human-in-the-loop referral automation across Reddit, YouTube, and Quora with Telegram oversight, monitoring, and recovery.

---

## 📋 Table of Contents
- [Overview](#overview)
- [Architecture Diagram](#architecture-diagram)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
  - [Windows](#windows-installation)
  - [Linux](#linux-installation)
  - [macOS](#macos-installation)
  - [Post-install Verification](#post-install-verification)
  - [Common Installation Errors & Fixes](#common-installation-errors--fixes)
- [Configuration Guide](#configuration-guide)
  - [Environment Variables](#environment-variables)
  - [configyaml Reference](#configyaml-reference)
  - [Platform Setup (Reddit, YouTube, Quora)](#platform-setup-reddit-youtube-quora)
  - [Security Best Practices](#security-best-practices)
- [Startup Validation & Health](#startup-validation--health)
- [Running the System](#running-the-system)
  - [Development Mode](#development-mode)
  - [Production Deployment](#production-deployment)
  - [Monitoring & Logs](#monitoring--logs)
  - [Start/Stop/Restart](#startstoprestart)
- [Resilience & Migrations](#resilience--migrations)
- [Telegram Bot Commands](#telegram-bot-commands)
- [Troubleshooting](#troubleshooting)
- [Testing Guide](#testing-guide)
- [Architecture Deep Dive](#architecture-deep-dive)
- [Development Guide](#development-guide)
- [FAQ](#faq)
- [Changelog](#changelog)

---

## Overview
The Referral Automation System orchestrates content discovery, generation, human review, posting, and performance tracking across Reddit, YouTube, and Quora. It blends automation with human-in-the-loop safeguards to keep accounts healthy, respect platform limits, and maintain high-quality engagement.

This system is designed for growth teams, marketers, and operators who need consistent referral traffic without micromanaging every post. It integrates a Telegram bot for approvals, alerts, and manual overrides, ensuring critical actions remain supervised while routine tasks stay automated.

Key benefits include multi-platform reach, robust monitoring (health checks, alerts, analytics), resilient recovery (CAPTCHA/2FA handling, crash recovery), and extensibility for new platforms or content templates. The architecture favors composability: adapters handle platform-specific logic, the orchestrator coordinates workflows, and utilities provide cross-cutting concerns like logging, configuration, and retry.

With a hardened production focus, the project includes deployment scripts (systemd, Docker, Windows service guidance), backup utilities, and a comprehensive troubleshooting guide that addresses real-world failure modes such as rate limits, CAPTCHA challenges, and shadowban detection.

---

## Architecture Diagram
```
                              ┌────────────────────────────┐
                              │        Operators           │
                              │ (Telegram chat + CLI)      │
                              └─────────────┬──────────────┘
                                            │
                                   Human-in-the-loop
                                            │
┌─────────────────────┐   Notifications   ┌───────────────────────┐
│   Monitoring/Alert  │◀──────────────────│   Telegram Controller │
│  (health, analytics │───Commands/Flows──│ (actions, approvals,  │
│   alerts, reports)  │                   │    file delivery)     │
└──────────┬──────────┘                   └──────────┬────────────┘
           │                                         │
           │                         Requests/Approvals/Inputs
           │                                         │
┌──────────▼──────────┐      Schedules/Tasks   ┌─────▼─────────────┐
│  System Orchestrator│────────────────────────▶│    Scheduler     │
│ (routines, routing, │                         │ (interval tasks) │
│  health, recovery)  │◀────────────────────────┤                  │
└──────────┬──────────┘  Metrics/State          └─────┬────────────┘
           │                                         │
           │                                         │
┌──────────▼──────────┐    Content/Accounts   ┌──────▼──────────────┐
│  Content Generator   │──────────────────────▶│ Account Manager     │
│ (templates, scoring) │                      │ (rotation, health)  │
└──────────┬──────────┘                      └──────┬──────────────┘
           │                                         │
           │                                         │
┌──────────▼──────────┐   Posts/Comments   ┌─────────▼──────────────┐
│ Platform Adapters   │────────────────────▶│   Database (SQLAlchemy)│
│ (Reddit, YouTube,   │◀────────────────────│  posts/accounts/health │
│  Quora, future)     │   Health & Metrics  │  metrics/interactions  │
└──────────┬──────────┘                     └───────────────────────┘
           │
           │ Anti-bot challenges (CAPTCHA/2FA/email)
           ▼
┌─────────────────────┐
│  Captcha Handler     │
│ (screenshots, human  │
│  solve via Telegram) │
└─────────────────────┘
```

---

## Prerequisites

### System Requirements
- OS: Windows 10/11, Ubuntu 20.04+/Debian 11+, macOS 12+
- Python: 3.10+ (3.8+ may work, but tooling and scripts are optimized for 3.10)
- RAM: 4 GB minimum (8 GB recommended when running Selenium-based Quora flows)
- Disk: 2 GB free for code, logs, cache; additional space for screenshots/backups
- Network: Stable outbound HTTPS; reachable Telegram API; platform APIs (Reddit/YouTube/Quora)

### External Services
- **Telegram Bot**: Bot token + operator chat ID
- **Reddit**: OAuth app (script type) with client_id, client_secret, username, password, user_agent
- **YouTube**: OAuth client (client_id, client_secret, refresh_token) and/or API key
- **Quora**: Selenium-driven login; email/password; 2FA/email verification access
- **Database**: SQLite by default; PostgreSQL/MySQL recommended for production

### Accounts & Access
- Create and verify platform accounts; enable 2FA where available
- For Reddit shadowban checks, ensure ability to post to r/ShadowBan
- For YouTube, ensure API quota is adequate (search ~100 units per call)
- For Quora, ensure CAPTCHA challenges can be solved via Telegram when prompted

---

## Installation

### Windows Installation
```powershell
# 1) Clone
git clone https://github.com/your-org/referral-automation.git
Set-Location referral-automation

# 2) Python env
py -3.10 -m venv venv
.\venv\Scripts\Activate.ps1

# 3) Dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt

# 4) Environment
Copy-Item .env.example .env
notepad .env  # fill in credentials

# 5) Verify
python -c "import src; print('OK')"
```
Common Windows notes:
- If PowerShell restricts scripts, run: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`
- If using WSL, follow Linux steps below inside WSL

### Linux Installation
```bash
git clone https://github.com/your-org/referral-automation.git
cd referral-automation

python3.10 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env
nano .env  # fill credentials

# Optional: install system deps for Selenium (Quora)
sudo apt-get update
sudo apt-get install -y chromium-browser chromium-chromedriver
```

### macOS Installation
```bash
git clone https://github.com/your-org/referral-automation.git
cd referral-automation

python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

cp .env.example .env
open -a TextEdit .env

# If using Homebrew ChromeDriver
brew install chromedriver
```

### Post-install Verification
```bash
# Inside venv
python -m pytest tests/unit/test_state_manager.py -q
python main.py --help
```
You should see the orchestrator help output and passing smoke tests.

### Common Installation Errors & Fixes
- **Python not found**: Ensure `python`/`py` points to 3.10+. Run `python --version`.
- **Pip SSL errors**: Upgrade certs (`/Applications/Python 3.x/Install Certificates.command` on macOS) or set corporate CA.
- **Chromedriver missing**: Install OS package or place matching driver in PATH; set `CHROMEDRIVER_PATH` if needed.
- **env vars missing**: If `.env` placeholders remain, config loading will fail. Fill required values.
- **Permission denied on logs/data**: Ensure `data/` and `logs/` are writable; on Linux `chmod -R u+rwX data logs`.

---

## Configuration Guide

### Environment Variables
Create `.env` from `.env.example`. Key entries:
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_USER_CHAT_ID`
- `DATABASE_URL` (override default sqlite if using Postgres/MySQL)
- `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`
- `YOUTUBE_API_KEY`, `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` (if used)
- `ENCRYPTION_KEY` for secure credential storage
- Optional tuning: `TEMPLATES_PATH`, `SYNONYMS_PATH`, `LOG_LEVEL`

Quick reference (fill all marked *required*):

| Variable | Required | Description | Example |
| --- | --- | --- | --- |
| TELEGRAM_BOT_TOKEN | yes | Bot token from BotFather | `123456:ABC...` |
| TELEGRAM_USER_CHAT_ID | yes | Operator chat ID to authorize commands | `123456789` |
| DATABASE_URL | recommended | SQLAlchemy URL (overrides config.yaml DB) | `postgresql://user:pass@localhost/db` |
| REDDIT_CLIENT_ID/SECRET | yes (if reddit enabled) | Reddit script app creds | `abcd1234` |
| REDDIT_USER_AGENT | yes | Descriptive UA string | `referral-bot/1.0 by u/yourname` |
| REDDIT_USERNAME/PASSWORD | yes | Account credentials (script app) | `myuser` |
| YOUTUBE_API_KEY | optional | For lightweight calls; still prefer OAuth | `AIza...` |
| YOUTUBE_CLIENT_ID/SECRET | yes (if youtube enabled) | OAuth client | `...apps.googleusercontent.com` |
| YOUTUBE_REFRESH_TOKEN | recommended | For long-lived YouTube sessions | `1//0g...` |
| ENCRYPTION_KEY | yes | 32+ char key for credential encryption | `base64...` |
| LOG_LEVEL | optional | Override logging level | `DEBUG` |
| TEMPLATES_PATH | optional | Custom template file path | `config/templates.yaml` |
| SYNONYMS_PATH | optional | Custom synonyms file path | `config/synonyms.yaml` |

### config.yaml Reference
The primary configuration lives in `config/config.yaml`. Highlights:
- `system`: name, version, timezone, `max_concurrent_tasks`
- `telegram`: notification level, timeouts, retries
- `database`: type, path/DSN, backups
- `platforms.reddit`: enable, OAuth, post limits, delays, quality threshold, subreddits
- `platforms.youtube`: enable, max_comments_per_day, `search_keywords`
- `platforms.quora`: enable, max answers, topics
- `content`: min/max length, quality threshold, public sources
- `monitoring`: health check interval, alert thresholds, retention
- `logging`: level, format, file path, rotation

Example (excerpt):
```yaml
telegram:
  bot_token: ${TELEGRAM_BOT_TOKEN}
  user_chat_id: ${TELEGRAM_USER_CHAT_ID}
  notification_level: "INFO"
  timeout_seconds: 3600

platforms:
  reddit:
    enabled: true
    oauth:
      client_id: ${REDDIT_CLIENT_ID}
      client_secret: ${REDDIT_CLIENT_SECRET}
      user_agent: ${REDDIT_USER_AGENT}
      username: ${REDDIT_USERNAME}
      password: ${REDDIT_PASSWORD}
    max_posts_per_day: 15
    quality_threshold: 0.7
  youtube:
    enabled: true
    search_keywords:
      - "passive income apps"
      - "make money online"
```

Full reference (annotated):
```yaml
system:
  name: "Referral Automation System"
  version: "1.0.0"
  timezone: "UTC"
  max_concurrent_tasks: 5

telegram:
  bot_token: ${TELEGRAM_BOT_TOKEN}
  user_chat_id: ${TELEGRAM_USER_CHAT_ID}
  notification_level: "INFO"
  timeout_seconds: 3600
  retry_attempts: 3
  max_messages_per_minute: 20

database:
  type: "sqlite"                 # or postgresql / mysql+pymysql
  path: "data/database.db"
  backup_enabled: true
  backup_interval_hours: 24

platforms:
  reddit:
    enabled: true
    oauth:
      client_id: ${REDDIT_CLIENT_ID}
      client_secret: ${REDDIT_CLIENT_SECRET}
      user_agent: ${REDDIT_USER_AGENT}
      username: ${REDDIT_USERNAME}
      password: ${REDDIT_PASSWORD}
    max_posts_per_day: 15
    min_delay_between_posts: 600
    max_delay_between_posts: 1800
    quality_threshold: 0.7
    subreddits:
      - "beermoney"
      - "passive_income"
      - "sidehustle"
  youtube:
    enabled: true
    max_comments_per_day: 20
    search_keywords:
      - "passive income apps"
      - "make money online"
      - "side hustle"
  quora:
    enabled: true
    max_answers_per_day: 5
    topics:
      - "Passive Income"
      - "Side Hustles"

content:
  min_length: 200
  max_length: 800
  quality_threshold: 0.7
  public_sources:
    - "https://www.usa.gov/agencies"
    - "https://archive.org/details/texts"

monitoring:
  health_check_interval: 300
  alert_thresholds:
    error_rate: 0.1
    ban_rate: 0.05
  metrics_retention_days: 90

retry:
  max_attempts: 3
  base_delay: 5
  max_delay: 300
  exponential_backoff: true

logging:
  level: "INFO"
  format: "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
  file_path: "data/logs/system.log"
  max_file_size_mb: 10
  backup_count: 5
```

Configuration verification checklist:
- Validate all required env vars are filled (no `${...}` placeholders left).
- Confirm directory permissions for `data/`, `logs/`, `screenshots/`, `backups/`.
- For Postgres/MySQL: test connectivity with `psql`/`mysql` before running the app.
- For Reddit: confirm script app is “installed app”/“script” type.
- For YouTube: ensure OAuth consent is completed and refresh token is valid.
- For Quora: verify credentials by manual login once; note if CAPTCHA appears.

### Platform Setup (Reddit, YouTube, Quora)
- **Reddit OAuth**: Create a “script” app at https://www.reddit.com/prefs/apps. Copy client_id/secret/user_agent. Ensure account can post; enable 2FA only if supported by automation strategy.
- **YouTube OAuth**: Create OAuth client (Desktop) in Google Cloud Console. Enable YouTube Data API v3. Obtain refresh token via OAuth consent flow; store in credentials store or `.env`.
- **Quora**: Use real user credentials. Expect CAPTCHA/2FA; the system will escalate challenges to Telegram with screenshots and inline actions (solve/skip/refresh).

### Security Best Practices
- Never commit real secrets. Use `.env` and secret managers.
- Limit Telegram bot access to a single operator chat_id.
- Set least-privilege DB credentials; enable SSL for Postgres/MySQL.
- Rotate API keys regularly; track in `CHANGELOG.md`.
- Store screenshots/backups under restricted permissions; purge old artifacts.
- Back up `ENCRYPTION_KEY` (stored in `.env` and `data/.encryption_key`, perms 600 POSIX).
- Ensure `data/` and subdirs (`logs/`, `backups/`, `screenshots/`) are writable only by the service user.

---

## Startup Validation & Health

### Pre-flight (blocking on start)
- Required env vars present and non-placeholder (Telegram, Reddit, YouTube, encryption key).
- Filesystem directories exist and writable: `data/`, `data/logs/`, `data/backups/`, `data/screenshots/`.
- Disk space warning if <1GB free.
- Database reachable; SQLite integrity check.
- Dependency imports validated.
- Telegram token format checked.

### Startup health verification (blocking)
- Database: `SELECT 1`, integrity (SQLite), latency warning.
- Telegram: send test notification (falls back to degraded logging mode if fails).
- Scheduler: running flag validated.
- Critical failures exit with code 1 (unless `--skip-validation` used).

### Runtime health (scheduled)
- Health checks every 5 minutes for database, telegram, disk, memory, platforms.
- Database unhealthy triggers graceful shutdown to avoid corruption.
- Degraded optional services (Telegram/adapters/monitoring) log warnings and auto-retry with backoff.

See `docs/HEALTH_CHECKS.md` for full details.

---

## Running the System

### Development Mode
```bash
source venv/bin/activate          # Windows: .\venv\Scripts\activate
python main.py --config config/config.yaml
```
Development mode uses polling Telegram bot, SQLite DB, and logs to `data/logs/system.log`.

### Production Deployment
Options:
1. **Systemd (Linux)**: Use `scripts/referral-automation.service` with `setup.sh`/`deploy.sh`.
2. **Docker**: Build via `Dockerfile` and orchestrate with `docker-compose.yml` for volumes/logs/env.
3. **Windows Service**: Use NSSM or `sc create` pointing to `python main.py`.

Key steps (Linux):
```bash
./scripts/setup.sh             # initial env, venv, deps, db init, health check
./scripts/deploy.sh --with-tests
sudo systemctl enable referral-automation.service
sudo systemctl start referral-automation.service
```

Docker quickstart:
```bash
docker build -t referral .
docker run -d --name referral -v $(pwd)/config:/app/config referral
```

Windows service (NSSM):
```powershell
nssm install ReferralAutomation "C:\path\to\python.exe" "C:\path\to\main.py"
nssm set ReferralAutomation AppDirectory "C:\path\to\referral-automation"
nssm set ReferralAutomation AppParameters "--config config\config.yaml"
nssm start ReferralAutomation
```

Verification after deployment:
- `systemctl status referral-automation.service` shows `active (running)` with recent logs.
- `docker ps` lists the container as `healthy` (see healthcheck in docker-compose).
- Telegram `/status` responds with uptime and pending actions.
- Logs show “Starting orchestrator” followed by “System started successfully”.

### Monitoring & Logs
- Health checks every 5 minutes (disk, memory, DB, Telegram, platform adapters).
- Alerts are sent to Telegram (rate-limited) for degraded/unhealthy components.
- Analytics report daily at 09:00 UTC with ASCII charts and recommendations.
- Logs: `data/logs/system.log` rotated; `logger` supports JSON/structured output with correlation IDs.

### Start/Stop/Restart
- **Systemd**: `sudo systemctl start|stop|restart referral-automation.service`
- **Docker**: `docker-compose up -d` / `docker-compose down`
- **Dev shell**: Ctrl+C or send `/pause` via Telegram; graceful shutdown persists state.

### Operations Runbook (fast actions)
- **Run health check now**: `/summary` or trigger monitoring task (future command `/health`).
- **Tail logs (Linux)**: `tail -n 200 -f data/logs/system.log`
- **Tail logs (Docker)**: `docker-compose logs -f --tail=200`
- **Rotate credentials**: update `.env`, restart service, verify `/status`.
- **Restore from backup (SQLite)**: stop service → copy backup into `data/database.db` → start service → run smoke tests.
- **Trigger daily analytics manually**: `/summary` (sends daily report immediately).
- **Pause automation**: `/pause`; resume with `/resume`.

---

## Telegram Bot Commands
Core commands (authorized chat only):
- `/help` — list commands
- `/status` — system and queue status
- `/pause` / `/resume` — pause/resume automation
- `/logs [level]` — tail logs; level filter optional
- `/approve <post_id>` — approve draft
- `/reject <post_id>` — reject draft
- `/edit <post_id>` — edit draft content inline
- `/quickreply <post_id>` — send quick reply template
- `/accounts` — list accounts and health
- `/account <id>` — details for account
- `/add_account` — guided add
- `/summary` — trigger daily analytics report
- Inline callback buttons: Approve/Reject/Edit, Run Health Check, View Detailed Stats, Download Full Report, Review Flagged Accounts

Human input flows:
- Content review prompts with inline buttons
- CAPTCHA solve / 2FA / verification email entry with screenshot delivery
- Health/alert acknowledgments via inline buttons

---

## Troubleshooting

### Common Issues (Symptoms → Diagnosis → Resolution)
1) **Telegram bot not responding**  
   - Symptoms: No replies to commands.  
   - Diagnosis: Check bot token, network, Telegram API reachability; inspect logs for `Unauthorized`.  
   - Solution: Verify `TELEGRAM_BOT_TOKEN`, restart service, ensure bot started with `/start`.

2) **Database locked errors (SQLite)**  
   - Symptoms: `database is locked` in logs.  
   - Diagnosis: Concurrent writes or long transactions.  
   - Solution: Retry with backoff; ensure WAL mode; for production use Postgres/MySQL.

3) **Reddit authentication failed**  
   - Symptoms: Login fails, `user.me returned None`.  
   - Diagnosis: Wrong OAuth creds or 2FA prompt.  
   - Solution: Recreate script app; verify username/password; check user_agent; handle 2FA via Telegram prompt.

4) **Content quality score too low**  
   - Symptoms: Drafts stuck awaiting approval.  
   - Diagnosis: Quality threshold too high or templates weak.  
   - Solution: Adjust `platforms.reddit.quality_threshold`; improve templates/synonyms; use Telegram edit flow.

5) **Rate limit errors (Reddit/YouTube/Telegram)**  
   - Symptoms: 429/`RATELIMIT`.  
   - Diagnosis: Hitting API ceilings.  
   - Solution: Increase delays; rotate accounts; reduce daily quotas; respect YouTube quota cost (100 units/search).

6) **Shadowban detection**  
   - Symptoms: Posts visible to self only; low engagement.  
   - Diagnosis: Run shadowban check; r/ShadowBan test; visibility analysis.  
   - Solution: Auto-disable flagged account; switch via AccountManager; alert in Telegram.

7) **CAPTCHA challenges**  
   - Symptoms: Selenium flow pauses; AntiBotChallengeError.  
   - Diagnosis: Screenshot sent to Telegram.  
   - Solution: Solve via Telegram inline flow; refresh screenshot if stale; ensure headless browser supports images.

8) **Memory/disk space issues**  
   - Symptoms: Health check shows degraded/unhealthy; OOM kills.  
   - Diagnosis: `psutil` metrics in health report.  
   - Solution: Free disk, rotate logs, lower concurrency, enlarge swap.

9) **Account health degraded**  
   - Symptoms: Posts disabled, health <0.3.  
   - Diagnosis: Check alerts; review health events.  
   - Solution: AccountManager disables; investigate bans; rotate; re-enable manually after recovery rules.

10) **Platform-specific errors**  
   - Reddit: 2FA prompts, `INVALID_GRANT`, subreddit access denied → verify scopes and subreddit existence.  
   - YouTube: `quotaExceeded`, OAuth refresh failures → ensure refresh token valid; reduce search frequency.  
   - Quora: CAPTCHA loop or verification emails → solve via Telegram; check email inbox; consider non-headless during verification.

11) **Database migration or schema drift**  
    - Symptoms: Missing columns/tables, SQL errors on startup.  
    - Diagnosis: Compare DB schema vs models; check migration history.  
    - Solution: Re-run migrations (if used) or re-init dev DB; for prod, apply migrations carefully and back up first.

12) **Database corruption (SQLite)**  
    - Symptoms: `database disk image is malformed`.  
    - Diagnosis: `sqlite3 database.db "PRAGMA integrity_check;"`.  
    - Solution: Restore latest backup from `data/backups/`; investigate unclean shutdowns; consider Postgres for prod.

13) **Network or proxy issues**  
    - Symptoms: Timeouts to APIs, Telegram `TimedOut`.  
    - Diagnosis: Check outbound firewall/proxy; test `curl https://api.telegram.org`.  
    - Solution: Allowlist Telegram/API endpoints; set HTTP(S) proxy env vars if required.

14) **Webhook/Callback failures (future webhook mode)**  
    - Symptoms: Missing updates or duplicated commands.  
    - Diagnosis: Check Telegram webhook URL reachability; TLS validity.  
    - Solution: Renew certificate; ensure correct public URL; fall back to polling mode.

15) **High error rates / alert storms**  
    - Symptoms: Frequent alerts in Telegram.  
    - Diagnosis: Inspect health report; correlate with rate limits or platform outages.  
    - Solution: Increase backoff, pause automation, rotate accounts, verify credentials; alert manager rate-limits per 15 minutes.

### Platform-Specific Deep Dives
- **Reddit**:  
  - Shadowban checks (r/ShadowBan, comment visibility, engagement heuristics) with confidence scoring.  
  - 2FA handled via Telegram code entry.  
  - Rate limits: client-side limiter + retry backoff.
- **YouTube**:  
  - OAuth refresh and search quota management; search mode via keywords.  
  - 2FA/OAuth issues surfaced to Telegram; retries with backoff.  
  - Respect search quota (100 units/search) and deduplicate results to save cost.
- **Quora**:  
  - Selenium-driven; CAPTCHA screenshots; manual solve flow.  
  - Verify cookies/session after solves; consider lowering headless if detection occurs.  
  - Email verification handled via Telegram code entry workflow.

### Platform-Specific Deep Dives
- **Reddit**:  
  - Shadowban checks (r/ShadowBan, comment visibility, engagement heuristics) with confidence scoring.  
  - 2FA handled via Telegram code entry.  
  - Rate limits: client-side limiter + retry backoff.
- **YouTube**:  
  - OAuth refresh and search quota management; search mode via keywords.  
  - 2FA/OAuth issues surfaced to Telegram; retries with backoff.
- **Quora**:  
  - Selenium-driven; CAPTCHA screenshots; manual solve flow.  
  - Verify cookies/session after solves; consider lowering headless if detection occurs.

### Database Issues
- **Corruption**: Restore from `data/backups/` (see `scripts/backup.sh`).  
- **Migration drift**: Re-run migrations or recreate schema with `init_db`.  
- **Backup/restore**: Use timestamped backups; validate with test restore before rotation.

---

## Testing Guide
```bash
pytest tests/ --cov=src --cov-report=html --cov-report=term
```
- Unit tests live in `tests/unit/`; integration tests in `tests/integration/`.
- Coverage target: >75% overall; module minimums: core>80%, content>85%, platforms>75%, telegram>80%, monitoring>80%, utils>90%, database>75%.
- To view HTML coverage: open `htmlcov/index.html`.
- Writing new tests: mock external APIs (Telegram/Reddit/YouTube), use fixtures for DB/session, test error paths and timeouts.
- CI/CD tips: run `pytest -n auto` (with pytest-xdist) to stay under 60s; use `--maxfail=1` for quick feedback in hooks.
- Coverage generation in CI: `pytest --cov=src --cov-report=xml --cov-report=term`; publish `coverage.xml` to your pipeline.
- Integration test notes: mock network calls; seed SQLite in `tests/fixtures`; assert Telegram payloads not sent to real chats.

---

## Architecture Deep Dive
- **System Orchestrator (`src/core/orchestrator.py`)**: wires config, logging, DB, Telegram, scheduler; runs daily routines; will schedule health checks and analytics reports; handles graceful shutdown and state persistence.
- **Scheduler (`src/core/scheduler.py`)**: lightweight asyncio scheduler for periodic tasks (daily routine, health checks, analytics).
- **Content**: `TemplateManager` + `ContentGenerator` produce drafts with quality scoring and synonym expansion.
- **Platform Adapters**: Reddit (PRAW), YouTube (Data API v3), Quora (Selenium). Each implements login, find targets, post comment/answer, metrics, health. CAPTCHA/2FA escalated to Telegram.
- **Captcha Handler (`src/platforms/captcha_handler.py`)**: captures screenshots, sends inline Telegram keyboards, waits for human responses with timeout; supports reCAPTCHA/hCAPTCHA/image/text/audio; cleans up artifacts.
- **Monitoring (`src/monitoring/*`)**: health checks (psutil/db/telegram/platform adapters), analytics aggregation, alert manager with rate limiting and Telegram notifications.
- **Account Manager (`src/core/account_manager.py`)**: selects best account, rotates on degradation, tracks health, disables/reactivates based on rules.
- **Utilities**: Config loader (validation, env expansion), structured logger (rotation, Telegram handler), retry and rate limiters, error recovery strategies.
- **Database**: SQLAlchemy models for Posts, Accounts, Health events, Telegram interactions, Metrics. Backups via `scripts/backup.sh`.

Data flow (simplified lifecycle):
1. Scheduler triggers daily routine → ContentGenerator drafts posts → stored in DB as pending.
2. Telegram prompts human review if below quality threshold → approvals/edits recorded.
3. When approved, adapter posts using AccountManager-selected account → AdapterResult captured.
4. Metrics and health events recorded; AccountManager adjusts health scores; Monitoring ingests for alerts.
5. Daily analytics aggregates posts/clicks/conversions/quality → Telegram report with recommendations.

Error handling strategy:
- Adapter errors isolated via `AdapterResult`.
- Retries with exponential backoff for transient failures.
- AntiBotChallengeError escalated to Telegram.
- Health/alert pipeline surfaces degraded/unhealthy components.
- Error recovery utilities suggest/attempt remediation (retry, account rotation, CAPTCHA solve).

State management:
- State persisted via `StateManager` (pause flag, timestamps).
- Pending Telegram actions rehydrated on startup.
- Account health stored and updated per operation.

---

## Development Guide
- **Adding new platforms**: Implement `BasePlatformAdapter` methods; integrate with AccountManager; add config section; wire into orchestrator/scheduler; add tests and README notes.
- **Extending content generation**: Add templates in `config/templates.yaml`; extend quality scoring; cache rendering for performance.
- **Custom commands**: Add Telegram handler in `src/telegram/handlers.py`; register in `controller.py`; document in README.
- **Monitoring extensions**: Add new health checks to `HealthChecker`; emit metrics via `Analytics`; define alert rules in `AlertManager`.
- **Code style**: Follow existing patterns; type hints required; small focused functions; structured logging with context.
- **Contribution workflow**:
  1. Branch from `main`.
  2. Add tests for every new feature/bugfix.
  3. Run `pytest --cov=src`.
  4. Update docs (`README`, `docs/*`, `CHANGELOG.md`).
  5. Use conventional commits.

Extending workflows:
- **Adding new Telegram flows**: create handler, register command/callback, use `request_human_input` for blocking flows, include inline keyboards.
- **New monitoring checks**: add method to `HealthChecker`, update overall weighting, add alert rule mapping in `AlertManager`.
- **Account health heuristics**: adjust scoring weights in AccountManager; ensure tests cover disable/reactivate paths.
- **Performance improvements**: profile DB queries; add indexes; cache template rendering; tune rate limiter thresholds.

---

## FAQ
1. **Is Telegram required?** Yes; it provides approvals, alerts, and human-in-the-loop solves.  
2. **Can I run without a browser?** Reddit/YouTube can run headless APIs; Quora requires Selenium browser.  
3. **What DB should I use in production?** PostgreSQL/MySQL. SQLite is fine for local/dev.  
4. **How do I handle 2FA?** Telegram prompts you to enter codes; the system retries with provided codes.  
5. **How are CAPTCHAs solved?** Screenshots are sent to Telegram with solve/refresh buttons; solutions are applied and retried.  
6. **Can I disable auto-posting?** Yes; keep `auto_post_after_approval=false` or run manual routines.  
7. **How do I rotate accounts?** AccountManager picks best accounts; rotation triggers when health <0.6 before posting.  
8. **What time are daily analytics sent?** 09:00 UTC by default via scheduler.  
9. **Where are logs?** `data/logs/system.log`, rotated; downloadable via `/logs` command.  
10. **How do I back up?** Run `scripts/backup.sh`; backups stored in `data/backups/`.  
11. **How to recover from crash?** State persists; run `test_crash_recovery` integration test; start orchestrator to reload state.  
12. **Why are quality scores low?** Update templates, synonyms, and thresholds; check content warnings.  
13. **How to add new keywords for YouTube search?** Add to `platforms.youtube.search_keywords` in config.  
14. **What triggers alerts?** Unhealthy/degraded health checks or critical overall score <0.5; rate-limited per component.  
15. **Can I run in Docker?** Yes; use provided Dockerfile and `docker-compose.yml` with mounted volumes for data/logs.

---

## Changelog
- See `CHANGELOG.md` for version history, breaking changes, and migration notes.

---

## License
MIT License. See `LICENSE`.
