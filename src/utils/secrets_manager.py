"""Centralized secrets loading with safe precedence and auditing.

Priority:
1) Environment variables
2) External secret provider (if configured)
3) .env file (local dev fallback)
4) Fallback files (e.g., data/.secrets_backup)

Secrets are never logged; only sources are recorded for auditability.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import stat
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Protocol


class SecretProvider(Protocol):
    """Optional external secret provider interface."""

    def get_secret(self, name: str) -> Optional[str]:
        ...


PLACEHOLDER_VALUES = {"", "REPLACE_ME", "CHANGE_ME", "TODO", "xxx", "XXXX", "REDACTED"}
DEFAULT_ENV_FILE = Path(os.getenv("APP_BASE_PATH", Path.cwd())) / ".env"
DEFAULT_FALLBACK_FILE = Path(os.getenv("APP_BASE_PATH", Path.cwd())) / "data" / ".secrets_backup"
EXPECTED_PERMS = 0o600
ENV_ENC_KEY_NAME = "SECRET_ENC_KEY"
ENV_HTTP_PROVIDER_URL = "SECRET_PROVIDER_URL"
ENV_HTTP_PROVIDER_TOKEN = "SECRET_PROVIDER_TOKEN"

LOGGER = logging.getLogger(__name__)


def _check_permissions(path: Path, label: str) -> None:
    """Warn if file permissions are more permissive than 600 on POSIX."""
    if os.name == "nt" or not path.exists():
        return
    perms = stat.S_IMODE(path.stat().st_mode)
    if perms & 0o077:
        LOGGER.warning("%s permissions too permissive (%o). Expected 600.", label, perms)


def _parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.exists():
        return values
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def _xor_crypt(data: bytes, key: bytes) -> bytes:
    if not key:
        raise ValueError("Encryption key missing")
    out = bytearray()
    for i, b in enumerate(data):
        out.append(b ^ key[i % len(key)])
    return bytes(out)


def encrypt_value(value: str, key: str) -> str:
    """Lightweight reversible encoding with XOR+base64 (placeholder until KMS)."""
    key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
    enc = _xor_crypt(value.encode("utf-8"), key_bytes)
    return "ENC::" + base64.urlsafe_b64encode(enc).decode("utf-8")


def _decrypt_value(value: str, key: str) -> str:
    key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
    raw = base64.urlsafe_b64decode(value.encode("utf-8"))
    dec = _xor_crypt(raw, key_bytes)
    return dec.decode("utf-8")


class SecretsManager:
    """Load secrets from multiple sources with explicit precedence."""

    def __init__(
        self,
        env_file_path: Path | None = None,
        fallback_files: Iterable[Path] | None = None,
        external_provider: SecretProvider | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.env_file_path = env_file_path or DEFAULT_ENV_FILE
        self.fallback_files = list(fallback_files) if fallback_files else [DEFAULT_FALLBACK_FILE]
        self.external_provider = external_provider or self._build_http_provider_from_env()
        self.logger = logger or LOGGER

    def get(
        self,
        name: str,
        required: bool = True,
        validator: Optional[Callable[[str], bool]] = None,
    ) -> Optional[str]:
        """Retrieve a secret using the defined hierarchy."""
        for source, loader in (
            ("environment", lambda: os.getenv(name)),
            ("external_provider", lambda: self._get_from_external(name)),
            (".env", lambda: self._get_from_env_file(name)),
        ):
            value = loader()
            if value is not None:
                return self._finalize_value(name, value, source, validator)

        for path in self.fallback_files:
            value = self._get_from_file(path, name)
            if value is not None:
                return self._finalize_value(name, value, str(path), validator)

        if required:
            raise ValueError(
                f"Missing required secret '{name}'. Provide via environment, secret manager, or .env."
            )
        return None

    def _finalize_value(
        self,
        name: str,
        value: str,
        source: str,
        validator: Optional[Callable[[str], bool]],
    ) -> str:
        decrypted = self._maybe_decrypt(value, source=source)
        if self._is_placeholder(decrypted):
            raise ValueError(f"Secret '{name}' from {source} appears to be a placeholder.")
        if validator and not validator(decrypted):
            raise ValueError(f"Secret '{name}' from {source} failed validation.")
        self._log_source(name, source)
        return decrypted

    def _get_from_external(self, name: str) -> Optional[str]:
        if not self.external_provider:
            return None
        try:
            return self.external_provider.get_secret(name)
        except Exception as exc:  # pragma: no cover - external providers may vary
            self.logger.error("External secret provider failed for %s: %s", name, exc)
            return None

    def _get_from_env_file(self, name: str) -> Optional[str]:
        values = _parse_env_file(Path(self.env_file_path))
        if name in values and values[name]:
            _check_permissions(Path(self.env_file_path), ".env")
            return values[name]
        return None

    def _get_from_file(self, path: Path, name: str) -> Optional[str]:
        values = _parse_env_file(path)
        if name in values and values[name]:
            _check_permissions(path, str(path))
            return values[name]
        return None

    def _is_placeholder(self, value: str) -> bool:
        normalized = (value or "").strip()
        return normalized in PLACEHOLDER_VALUES or normalized.lower() in {"changeme", "replace_me", "xxx"}

    def _maybe_decrypt(self, value: str, *, source: str) -> str:
        """Decrypt value if prefixed with ENC:: using SECRET_ENC_KEY."""
        if not isinstance(value, str) or not value.startswith("ENC::"):
            return value
        enc_key = os.getenv(ENV_ENC_KEY_NAME)
        if not enc_key:
            raise ValueError(f"Encrypted secret from {source} but SECRET_ENC_KEY not set.")
        payload = value.split("ENC::", 1)[1]
        try:
            return _decrypt_value(payload, enc_key)
        except Exception as exc:
            raise ValueError(f"Failed to decrypt secret from {source}: {exc}") from exc

    def _log_source(self, name: str, source: str) -> None:
        self.logger.info("Loaded secret '%s' from %s", name, source)

    def _build_http_provider_from_env(self) -> Optional[SecretProvider]:
        url = os.getenv(ENV_HTTP_PROVIDER_URL)
        if not url:
            return None
        token = os.getenv(ENV_HTTP_PROVIDER_TOKEN)

        class _HttpSecretProvider:
            def get_secret(self, name: str) -> Optional[str]:
                req_url = f"{url.rstrip('/')}/secret?name={name}"
                req = urllib.request.Request(req_url)
                if token:
                    req.add_header("Authorization", f"Bearer {token}")
                with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
                    payload = resp.read().decode("utf-8")
                    try:
                        data = json.loads(payload)
                        if isinstance(data, dict):
                            return data.get("value") or data.get("secret") or data.get(name)
                    except Exception:
                        return payload.strip() or None
                return None

        return _HttpSecretProvider()
