import asyncio
from unittest.mock import MagicMock

import pytest

from src.core.orchestrator import SystemOrchestrator


@pytest.mark.asyncio
async def test_orchestrator_init_and_shutdown(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("""
telegram:
  bot_token: dummy
  user_chat_id: 123
logging:
  level: INFO
  file_path: logs/test.log
  format: text
  max_file_size_mb: 5
  backup_count: 2
platforms:
  reddit:
    enabled: false
  youtube:
    enabled: false
  quora:
    enabled: false
system:
  environment: test
  auto_start: false
retry:
  max_attempts: 1
  backoff_seconds: 1
monitoring:
  enabled: false
content:
  templates_path: config/templates.yaml
  synonyms_path: config/synonyms.yaml
  max_comment_length: 500
  min_quality_score: 0.5
  paraphrase: false
  paraphrase_attempts: 1
  quality_weight: 1.0
  random_seed: 1
  word_range:
    min: 5
    max: 20
database:
  type: sqlite
  path: "{db_path}"
""".format(db_path=str(tmp_path / "test.db")))

    orch = SystemOrchestrator(config_path=str(config_path))

    # Mock telegram start/stop to avoid network
    fake_tel = MagicMock()
    orch.telegram = fake_tel

    await orch.graceful_shutdown()
    assert orch._shutdown_event.is_set()
