# Contributing Guide

## Requirements
- Python 3.10+.
- Install dependencies: `pip install -r requirements-dev.txt` (if available).
- Formatters/linters: `ruff`, `black` (if configured).

## Workflow
1. **Fork & branch**: use descriptive feature branches (`feature/monitoring-alerts`).
2. **Environment**: copy `config/config.sample.yaml`, fill secrets, set `PYTHONPATH=.`.
3. **Testing**: run unit tests before pushing:
   ```bash
   pytest tests/unit
   ```
4. **Code style**: follow existing patterns; prefer small, focused commits.
5. **Docs**: update relevant markdown (README, CONFIGURATION, ARCHITECTURE, DEPLOYMENT, PRODUCTION_CHECKLIST) when behavior changes.
6. **PR template** (recommended):
   - Summary
   - Testing
   - Screenshots/logs if applicable
   - Checklist: [ ] tests [ ] docs [ ] config updates

## Guidelines
- Avoid rewriting stable components; extend incrementally.
- Keep adapter-specific logic within each `*_adapter.py`.
- Use `AccountManager` for account selection/rotation.
- Ensure human-in-the-loop flows respect Telegram response timeouts.
- Log structured data (use `logger.getChild`).
- For new monitoring alerts, ensure rate limiting in `AlertManager`.
