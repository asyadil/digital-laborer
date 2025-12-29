import os
from pathlib import Path

from src.utils.crypto import _resolve_key, ENV_FILE_PATH, KEY_FILE_PATH


def test_generate_and_persist_key(tmp_path, monkeypatch):
    # point paths to temp dir
    monkeypatch.setenv("APP_BASE_PATH", str(tmp_path))
    env_path = Path(tmp_path) / ".env"
    key_path = Path(tmp_path) / "data" / ".encryption_key"
    monkeypatch.setattr("src.utils.crypto.ENV_FILE_PATH", env_path)
    monkeypatch.setattr("src.utils.crypto.KEY_FILE_PATH", key_path)

    key = _resolve_key()

    assert key
    assert env_path.exists()
    assert key_path.exists()
    assert f"ENCRYPTION_KEY={key}" in env_path.read_text()
    assert key_path.read_text().strip() == key


def test_reuse_key_from_env(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    env_key = Fernet.generate_key().decode()
    monkeypatch.setenv("ENCRYPTION_KEY", env_key)
    monkeypatch.setenv("APP_BASE_PATH", str(tmp_path))
    env_path = Path(tmp_path) / ".env"
    key_path = Path(tmp_path) / "data" / ".encryption_key"
    monkeypatch.setattr("src.utils.crypto.ENV_FILE_PATH", env_path)
    monkeypatch.setattr("src.utils.crypto.KEY_FILE_PATH", key_path)

    key = _resolve_key()
    assert key == env_key
    assert not env_path.exists()  # should not write when env provided
