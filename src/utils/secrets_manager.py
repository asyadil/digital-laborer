"""Centralized secrets loading with safe precedence and auditing.

Priority:
1) Environment variables
2) External secret provider (if configured)
3) .env file (local dev fallback)
4) Fallback files (e.g., data/.secrets_backup)

Secrets are never logged; only sources are recorded for auditability.
"""
from __future__ import annotations

import logging
import os
import stat
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
        self.external_provider = external_provider
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
        if self._is_placeholder(value):
            raise ValueError(f"Secret '{name}' from {source} appears to be a placeholder.")
        if validator and not validator(value):
            raise ValueError(f"Secret '{name}' from {source} failed validation.")
        self._log_source(name, source)
        return value

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

    def _log_source(self, name: str, source: str) -> None:
        self.logger.info("Loaded secret '%s' from %s", name, source)
