# Changelog

## Unreleased
### Added
- Monitoring system:
  - `HealthChecker` with DB/Telegram/disk/memory/platform checks.
  - `AlertManager` for rate-limited Telegram alerts.
  - `Analytics` aggregations and ASCII Telegram reports (daily & weekly).
- CAPTCHA/2FA handler integrated with Telegram for Quora, Reddit, YouTube adapters.
- Account management layer with rotation, health scoring, disable/reactivate.
- YouTube keyword search pipeline with relevance scoring.
- Shadowban detection heuristics in Reddit adapter.
- Documentation: CONFIGURATION, ARCHITECTURE, DEPLOYMENT, PRODUCTION_CHECKLIST, CONTRIBUTING.

### Changed
- Orchestrator now schedules health checks (5m), daily/weekly analytics, multi-platform daily routine, and auto-post with account selection.
- Auto-post uses account rotation fallback and posts telemetry to Telegram.
- Enhanced Telegram controller for human review, custom inputs, and file sending.

### Fixed
- Improved error handling for Reddit login/2FA and YouTube token refresh.
- Config validation fails fast on missing critical fields.
