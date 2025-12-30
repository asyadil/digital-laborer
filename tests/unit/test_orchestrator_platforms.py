import os
from pathlib import Path

import pytest

from src.core.orchestrator import SystemOrchestrator


def _write_minimal_config(tmp_path: Path) -> Path:
    logs_dir = tmp_path / "logs"
    data_dir = tmp_path / "data"
    logs_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        f"""
telegram:
  bot_token: dummy
  user_chat_id: "123"
logging:
  level: INFO
  file_path: "{(logs_dir / 'test.log').as_posix()}"
  format: "%(asctime)s %(levelname)s %(name)s %(message)s"
  max_file_size_mb: 5
  backup_count: 1
database:
  type: sqlite
  path: "{(data_dir / 'test.db').as_posix()}"
platforms:
  reddit:
    enabled: false
  youtube:
    enabled: false
  quora:
    enabled: false
  tiktok:
    enabled: true
    max_comments_per_day: 10
    min_delay_between_comments: 45
    max_delay_between_comments: 90
  instagram:
    enabled: true
    min_delay_between_comments: 45
    max_delay_between_comments: 90
  facebook:
    enabled: true
    min_delay_between_comments: 60
    max_delay_between_comments: 120
content:
  default_locale: "en"
""",
        encoding="utf-8",
    )
    return cfg_path


def test_platform_limiters_include_new_platforms(tmp_path: Path):
    cfg_path = _write_minimal_config(tmp_path)
    orch = SystemOrchestrator(config_path=str(cfg_path), skip_validation=True)
    assert {"tiktok", "instagram", "facebook"}.issubset(set(orch.platform_limiters.keys()))


def test_init_platform_adapters_sets_health(tmp_path: Path):
    cfg_path = _write_minimal_config(tmp_path)
    orch = SystemOrchestrator(config_path=str(cfg_path), skip_validation=True)
    orch._init_platform_adapters()
    assert orch.tiktok_adapter is not None
    assert orch.instagram_adapter is not None
    assert orch.facebook_adapter is not None
    assert orch.service_health.get("tiktok") == "healthy"
    assert orch.service_health.get("instagram") == "healthy"
    assert orch.service_health.get("facebook") == "healthy"
