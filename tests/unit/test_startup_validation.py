import os
from pathlib import Path

from sqlalchemy import create_engine

from src.core.startup_validation import run_preflight_checks
from src.utils.config_loader import AppConfig, TelegramConfig, DatabaseConfig


class DummyConfig:
    def __init__(self, db_path: str):
        self.telegram = TelegramConfig(bot_token="123:abcdefghijklmnopqrstuvwxyz123456", user_chat_id="1")
        self.database = DatabaseConfig(path=db_path)
        # minimal required sections
        self.platforms = type(
            "P",
            (),
            {
                "reddit": type("R", (), {"enabled": False})(),
                "youtube": type("Y", (), {"enabled": False})(),
            },
        )()


def test_preflight_missing_env(tmp_path, monkeypatch):
    db_path = tmp_path / "db.sqlite"
    engine = create_engine(f"sqlite:///{db_path}")
    config = DummyConfig(str(db_path))

    # Clear required envs to force errors
    for key in [
        "ENCRYPTION_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_USER_CHAT_ID",
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USERNAME",
        "REDDIT_PASSWORD",
        "REDDIT_USER_AGENT",
    ]:
        monkeypatch.delenv(key, raising=False)

    report = run_preflight_checks(config=config, engine=engine, base_path=Path(tmp_path))
    assert report.errors, "Expected errors when env vars are missing"
