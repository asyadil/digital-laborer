# Architecture Overview

## High-Level Components
- **Orchestrator** (`src/core/orchestrator.py`): entrypoint wiring config, logging, DB, Telegram, scheduling (daily routine, analytics, health checks), and auto-post flows.
- **Content Engine** (`src/content/generator.py`): deterministic template-based generation, paraphrasing, quality scoring.
- **Platform Adapters** (`src/platforms/*_adapter.py`): Reddit (API), YouTube (Data API v3), Quora (Selenium); all inherit BasePlatformAdapter.
- **Captcha/2FA Handler** (`src/platforms/captcha_handler.py`): human-in-the-loop via Telegram for CAPTCHA/OTP.
- **Monitoring** (`src/monitoring/*`): health checks, analytics aggregation, alerting.
- **Account Management** (`src/core/account_manager.py`): rotation, health-based disabling/reactivation.
- **Telegram Controller** (`src/telegram/controller.py`): notifications, interactive prompts, file send, human review.
- **Database Layer** (`src/database/*`): SQLAlchemy models and session manager.

## Data Flow
1. **Scheduling**: Orchestrator schedules daily routine (drafts), analytics (daily/weekly), health checks (5m).
2. **Content Drafting**:
   - Reddit: generate via templates → save Post(pending) → optional human review via Telegram → APPROVED.
   - YouTube: keyword search → generate comment → save Post(pending) → optional human review.
3. **Auto-Post** (optional per platform): On APPROVED, orchestrator selects account (AccountManager), initializes adapter, finds target, posts, updates Post status/metadata, notifies Telegram.
4. **Monitoring**:
   - HealthChecker runs checks (DB, Telegram, disk, memory, adapters) → AlertManager (rate-limited) → Telegram warnings.
   - Analytics aggregates posts/clicks/conversions/quality → formatted ASCII report → Telegram with inline buttons.
5. **Captcha/2FA**: Adapters delegate challenges to CaptchaHandler; screenshots/OTP requests sent to Telegram; user replies -> handler returns solution synchronously to adapter.

## Key Interactions
- Orchestrator ↔ Telegram: notifications, human review, captcha/OTP input.
- Orchestrator ↔ Adapters: login/post/find/health; adapter uses RateLimiter + Retry wrappers.
- Adapters ↔ AccountManager/DB: select healthiest account, update health/last_used, flag on failures or shadowban detection.
- HealthChecker ↔ AlertManager ↔ Telegram: health alerts with cooldown.

## Error Handling & Resilience
- Config validation at startup (orchestrator).
- DB session_scope with retries on lock.
- Rate limiting + exponential backoff in adapters.
- Health-based account disabling + reactivation.
- Telegram fallbacks to text when rich UI not available.

## Extensibility
- Add platform adapters by implementing BasePlatformAdapter contract.
- Extend monitoring by adding checks in HealthChecker.
- Add analytics dimensions in `monitoring/analytics.py`.

## Deployment Notes
- SQLite default; configure path under `database.path`.
- Telegram credentials required for human-in-the-loop flows.
- See CONFIGURATION.md and deployment docs for systemd/docker scripts (to be added).
