import os

import pytest
import yaml
from pydantic import ValidationError

from src.utils.config_loader import AppConfig, ConfigManager


def _write_config(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return path


def test_env_substitution(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setenv("TELEGRAM_USER_CHAT_ID", "chat456")
    data = {
        "telegram": {
            "bot_token": "${TELEGRAM_BOT_TOKEN}",
            "user_chat_id": "${TELEGRAM_USER_CHAT_ID}",
        }
    }
    path = _write_config(tmp_path, data)
    manager = ConfigManager(config_path=str(path))
    cfg: AppConfig = manager.config
    assert cfg.telegram.bot_token == "token123"
    assert cfg.telegram.user_chat_id == "chat456"
    assert cfg.telegram.mode in {"polling", "webhook"}


def test_invalid_logging_level(tmp_path):
    data = {
        "telegram": {"bot_token": "abc", "user_chat_id": "u"},
        "logging": {"level": "INVALID"},
    }
    path = _write_config(tmp_path, data)
    with pytest.raises((ValidationError, ValueError)):
        ConfigManager(config_path=str(path)).config


def test_platform_defaults_and_locale(tmp_path):
    data = {
        "telegram": {"bot_token": "abc", "user_chat_id": "u"},
        "platforms": {
            "tiktok": {"enabled": True, "preferred_location": "id"},
            "instagram": {"enabled": False},
            "facebook": {"enabled": True, "min_delay_between_comments": 30, "max_delay_between_comments": 60},
        },
        "content": {"default_locale": "id"},
    }
    path = _write_config(tmp_path, data)
    cfg = ConfigManager(config_path=str(path)).config
    assert cfg.platforms.tiktok.enabled is True
    assert cfg.platforms.tiktok.preferred_location == "id"
    assert cfg.platforms.instagram.enabled is False
    assert cfg.platforms.facebook.min_delay_between_comments == 30
    assert cfg.content.default_locale == "id"


def test_default_locale_validation(tmp_path):
    data = {
        "telegram": {"bot_token": "abc", "user_chat_id": "u"},
        "content": {"default_locale": "fr"},
    }
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        ConfigManager(config_path=str(path)).config


def test_min_max_delay_validation(tmp_path):
    data = {
        "telegram": {"bot_token": "abc", "user_chat_id": "u"},
        "platforms": {
            "tiktok": {"enabled": True, "min_delay_between_comments": 100, "max_delay_between_comments": 10},
        },
    }
    path = _write_config(tmp_path, data)
    with pytest.raises(ValidationError):
        ConfigManager(config_path=str(path)).config


def test_missing_env_var_raises(monkeypatch, tmp_path):
    data = {
        "telegram": {"bot_token": "${MISSING_ENV}", "user_chat_id": "123"},
    }
    path = _write_config(tmp_path, data)
    monkeypatch.delenv("MISSING_ENV", raising=False)
    with pytest.raises(ValueError):
        ConfigManager(config_path=str(path)).config
