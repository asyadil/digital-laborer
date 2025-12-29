"""Secure credential encryption using Fernet with persistent key management."""
from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cryptography.fernet import Fernet

LOGGER = logging.getLogger(__name__)

ENV_VAR_NAME = "ENCRYPTION_KEY"
ENV_FILE_PATH = Path(os.getenv("APP_BASE_PATH", Path.cwd())) / ".env"
KEY_FILE_PATH = Path(os.getenv("APP_BASE_PATH", Path.cwd())) / "data" / ".encryption_key"
EXPECTED_PERMS = 0o600


class EncryptionKeyError(RuntimeError):
    """Raised when encryption key handling fails."""


def _validate_key(raw_key: str) -> str:
    """Ensure the provided key is a valid Fernet key string."""
    if not raw_key or not isinstance(raw_key, str):
        raise EncryptionKeyError("Encryption key is missing or invalid.")
    try:
        Fernet(raw_key.encode())
    except Exception as exc:  # cryptography raises ValueError/TypeError
        raise EncryptionKeyError(
            "Invalid encryption key format. Ensure a valid Fernet key is provided."
        ) from exc
    return raw_key


def _check_permissions(path: Path, label: str) -> None:
    """Warn if file permissions are more permissive than 600 on POSIX."""
    if os.name == "nt" or not path.exists():
        return
    mode = path.stat().st_mode
    perms = stat.S_IMODE(mode)
    if perms & 0o077:
        LOGGER.warning(
            "%s permissions too permissive (%o). Expected 600.",
            label,
            perms,
        )


def _load_key_from_env() -> Optional[str]:
    key = os.getenv(ENV_VAR_NAME)
    if key:
        return _validate_key(key.strip())
    return None


def _parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                k, v = stripped.split("=", 1)
                values[k.strip()] = v.strip()
    except Exception as exc:
        raise EncryptionKeyError(f"Failed to read .env file at {path}: {exc}") from exc
    return values


def _load_key_from_env_file(path: Path) -> Optional[str]:
    env_values = _parse_env_file(path)
    if ENV_VAR_NAME in env_values and env_values[ENV_VAR_NAME]:
        _check_permissions(path, ".env")
        return _validate_key(env_values[ENV_VAR_NAME])
    return None


def _load_key_from_keyfile(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        _check_permissions(path, "encryption key file")
        content = path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        raise EncryptionKeyError(f"Failed to read encryption key file at {path}: {exc}") from exc
    if not content:
        return None
    return _validate_key(content)


def _persist_to_env_file(path: Path, key: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    env_values = _parse_env_file(path)
    if ENV_VAR_NAME in env_values and env_values[ENV_VAR_NAME] and env_values[ENV_VAR_NAME] != key:
        raise EncryptionKeyError(
            f"Encryption key in {path} differs from generated key. Resolve manually to continue."
        )
    env_values[ENV_VAR_NAME] = key
    lines: List[str] = []
    for k, v in env_values.items():
        lines.append(f"{k}={v}")
    try:
        with path.open("w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        if os.name != "nt":
            path.chmod(EXPECTED_PERMS)
    except Exception as exc:
        raise EncryptionKeyError(f"Failed to persist encryption key to {path}: {exc}") from exc


def _persist_to_key_file(path: Path, key: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            f.write(key + "\n")
        if os.name != "nt":
            path.chmod(EXPECTED_PERMS)
    except Exception as exc:
        raise EncryptionKeyError(f"Failed to persist encryption key to backup file {path}: {exc}") from exc


def _resolve_key() -> str:
    """Load encryption key from env, .env, or key file; generate if missing."""
    sources: List[Tuple[str, str]] = []

    env_key = _load_key_from_env()
    if env_key:
        sources.append(("environment", env_key))

    env_file_key = _load_key_from_env_file(ENV_FILE_PATH)
    if env_file_key:
        sources.append((".env", env_file_key))

    file_key = _load_key_from_keyfile(KEY_FILE_PATH)
    if file_key:
        sources.append(("data/.encryption_key", file_key))

    unique_keys = {value for _, value in sources}
    if len(unique_keys) > 1:
        source_names = ", ".join(src for src, _ in sources)
        raise EncryptionKeyError(
            f"Encryption key mismatch between sources ({source_names}). "
            "Align them to a single value before starting."
        )

    if unique_keys:
        # honor priority: environment > .env > key file
        for preferred in ("environment", ".env", "data/.encryption_key"):
            for src, val in sources:
                if src == preferred:
                    return val

    # No key found: generate and persist
    new_key = Fernet.generate_key().decode()
    _persist_to_env_file(ENV_FILE_PATH, new_key)
    _persist_to_key_file(KEY_FILE_PATH, new_key)

    LOGGER.warning(
        "Generated new encryption key and stored to %s and %s. Keep secure and back up.",
        ENV_FILE_PATH,
        KEY_FILE_PATH,
    )
    print(
        "⚠️ Generated new encryption key and stored to .env and data/.encryption_key. "
        "Back up this key to prevent data loss."
    )
    return new_key


class CredentialManager:
    def __init__(self) -> None:
        key = _resolve_key()
        self.cipher = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        """Encrypt credentials."""
        return self.cipher.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt credentials."""
        return self.cipher.decrypt(ciphertext.encode()).decode()


# Global instance
credential_manager = CredentialManager()
