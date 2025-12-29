# Configuration Guide

This document lists required settings for the referral automation system. All configuration values live in `config/config.yaml` (or environment overrides). The orchestrator validates critical fields at startup and will fail fast if missing.

## Core
- `logging.level` — e.g., `INFO`.
- `logging.file_path` — log file output.
- `database.path` — SQLite DB path (required).

## Telegram (required)
- `telegram.bot_token` — Bot token for notifications and human-in-the-loop flows.
- `telegram.user_chat_id` — Chat ID to receive prompts and alerts.
- `telegram.timeout_seconds` — Timeout for human review/CAPTCHA input.

## Platforms
### Reddit
- `platforms.reddit.enabled` — `true|false`.
- `platforms.reddit.max_posts_per_day` — draft volume.
- `platforms.reddit.quality_threshold` — human review threshold.
- `platforms.reddit.auto_post_after_approval` — enable auto-post.
- `platforms.reddit.subreddits` — list of target subreddits.
- `platforms.reddit.oauth.client_id`
- `platforms.reddit.oauth.client_secret`
- `platforms.reddit.oauth.user_agent`
- `platforms.reddit.oauth.username`
- `platforms.reddit.oauth.password`

### YouTube
- `platforms.youtube.enabled` — `true|false`.
- `platforms.youtube.max_comments_per_day`
- `platforms.youtube.search_keywords` — list for discovery.
- `platforms.youtube.quality_threshold`
- `platforms.youtube.client_id`
- `platforms.youtube.client_secret`
- `platforms.youtube.refresh_token` (if using OAuth refresh).

### Quora
- `platforms.quora.enabled`
- `platforms.quora.timeout_seconds`
- Credentials are supplied via secure storage; CAPTCHA handler uses Telegram.

## Monitoring
- `monitoring.health.check_interval_seconds`
- `monitoring.alerts.cooldown_seconds`
- `monitoring.analytics.enabled`

## Content
- `content.min_length` / `content.max_length`
- `templates_path` and `synonyms_path` (env overrides) — defaults to `config/templates.yaml` and `config/synonyms.yaml`.

## Secrets Handling
Do not store secrets in VCS. Use environment overrides or encrypted storage. The system expects encrypted passwords for DB-backed accounts; see `src/utils/crypto/credential_manager`.
