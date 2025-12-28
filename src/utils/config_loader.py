"""Configuration management with validation and live reload support."""
from __future__ import annotations

import logging
import os
import re
import signal
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, root_validator, validator

_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


def _expand_env_value(value: str) -> str:
    matches = list(_ENV_PATTERN.finditer(value))
    for match in matches:
        env_key = match.group(1)
        env_val = os.getenv(env_key)
        if env_val is None:
            raise ValueError(f"Missing required environment variable: {env_key}")
        value = value.replace(match.group(0), env_val)
    return value


def _resolve_env_vars(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    if isinstance(obj, str):
        return _expand_env_value(obj) if "${" in obj else obj
    return obj


class SystemConfig(BaseModel):
    name: str = "Referral Automation System"
    version: str = "1.0.0"
    timezone: str = "UTC"
    max_concurrent_tasks: int = Field(default=5, ge=1)


class TelegramConfig(BaseModel):
    bot_token: str
    user_chat_id: str
    notification_level: str = "INFO"
    mode: str = "polling"
    max_messages_per_minute: int = Field(default=20, ge=1, le=120)
    timeout_seconds: int = Field(default=3600, ge=60)
    retry_attempts: int = Field(default=3, ge=0)

    @validator("notification_level")
    def validate_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in allowed:
            raise ValueError(f"Invalid notification level: {v}")
        return v.upper()

    @validator("mode")
    def validate_mode(cls, v: str) -> str:
        allowed = {"polling", "webhook"}
        mode = v.lower().strip()
        if mode not in allowed:
            raise ValueError(f"Invalid telegram mode: {v}")
        return mode


class DatabaseConfig(BaseModel):
    type: str = "sqlite"
    path: str = "data/database.db"
    backup_enabled: bool = True
    backup_interval_hours: int = Field(default=24, ge=1)


class RedditPlatformConfig(BaseModel):
    enabled: bool = True
    oauth: Dict[str, str] = Field(default_factory=dict)
    auto_post_after_approval: bool = Field(
        default=False,
        description="Auto-post to Reddit after human approval. False means manual trigger only."
    )
    max_posts_per_day: int = Field(default=15, ge=0)
    min_delay_between_posts: int = Field(default=600, ge=0)
    max_delay_between_posts: int = Field(default=1800, ge=0)
    quality_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    subreddits: list[str] = Field(default_factory=list)

    @root_validator
    def validate_delays(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        min_delay = values.get("min_delay_between_posts", 0)
        max_delay = values.get("max_delay_between_posts", 0)
        if max_delay and min_delay and max_delay < min_delay:
            raise ValueError("max_delay_between_posts cannot be less than min_delay_between_posts")
        return values


class YoutubePlatformConfig(BaseModel):
    enabled: bool = True
    max_comments_per_day: int = Field(default=20, ge=0)
    search_keywords: list[str] = Field(default_factory=list)


class QuoraPlatformConfig(BaseModel):
    enabled: bool = True
    max_answers_per_day: int = Field(default=5, ge=0)
    topics: list[str] = Field(default_factory=list)


class PlatformsConfig(BaseModel):
    reddit: RedditPlatformConfig = Field(default_factory=RedditPlatformConfig)
    youtube: YoutubePlatformConfig = Field(default_factory=YoutubePlatformConfig)
    quora: QuoraPlatformConfig = Field(default_factory=QuoraPlatformConfig)


class ContentConfig(BaseModel):
    min_length: int = Field(default=200, ge=50)
    max_length: int = Field(default=800, ge=50)
    quality_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    public_sources: list[str] = Field(default_factory=list)

    @root_validator
    def validate_lengths(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        min_len = values.get("min_length", 0)
        max_len = values.get("max_length", 0)
        if max_len and min_len and max_len < min_len:
            raise ValueError("max_length cannot be less than min_length")
        return values


class MonitoringConfig(BaseModel):
    health_check_interval: int = Field(default=300, ge=30)
    alert_thresholds: Dict[str, float] = Field(default_factory=lambda: {"error_rate": 0.1, "ban_rate": 0.05})
    metrics_retention_days: int = Field(default=90, ge=1)


class RetryConfig(BaseModel):
    max_attempts: int = Field(default=3, ge=1)
    base_delay: int = Field(default=5, ge=0)
    max_delay: int = Field(default=300, ge=0)
    exponential_backoff: bool = True


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    file_path: str = "data/logs/system.log"
    max_file_size_mb: int = Field(default=10, ge=1)
    backup_count: int = Field(default=5, ge=1)

    @validator("level")
    def validate_logging_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        level = v.upper()
        if level not in allowed:
            raise ValueError(f"Invalid logging level: {v}")
        return level


class AppConfig(BaseModel):
    system: SystemConfig = Field(default_factory=SystemConfig)
    telegram: TelegramConfig
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    platforms: PlatformsConfig = Field(default_factory=PlatformsConfig)
    content: ContentConfig = Field(default_factory=ContentConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


class ConfigManager:
    """Load, validate, and hot-reload configuration files."""

    def __init__(self, config_path: str, logger: Optional[logging.Logger] = None) -> None:
        self.config_path = config_path
        self.logger = logger or logging.getLogger("config_manager")
        self._config: Optional[AppConfig] = None
        self._lock = threading.RLock()
        self._last_loaded_at: Optional[datetime] = None
        self._sighup_registered = False
        self.load()

    @property
    def config(self) -> AppConfig:
        if self._config is None:
            raise RuntimeError("Configuration has not been loaded.")
        return self._config

    def load(self) -> AppConfig:
        """Load and validate configuration from YAML."""
        with self._lock:
            try:
                raw_config = self._read_config_file(self.config_path)
                resolved_config = _resolve_env_vars(raw_config)
                self._config = AppConfig(**resolved_config)
                self._last_loaded_at = datetime.now(timezone.utc)
                self.logger.info(
                    "Configuration loaded successfully",
                    extra={"component": "config", "path": self.config_path},
                )
                return self._config
            except (FileNotFoundError, ValidationError, yaml.YAMLError, ValueError) as exc:
                self.logger.error(
                    "Failed to load configuration",
                    extra={"component": "config", "path": self.config_path, "error": str(exc)},
                )
                raise

    def reload(self, *_args: Any, **_kwargs: Any) -> AppConfig:
        """Reload configuration (intended for SIGHUP)."""
        self.logger.info(
            "Reloading configuration", extra={"component": "config", "path": self.config_path}
        )
        return self.load()

    def register_reload_on_sighup(self) -> None:
        if self._sighup_registered:
            return
        try:
            signal.signal(signal.SIGHUP, self.reload)
            self._sighup_registered = True
            self.logger.info("Registered SIGHUP handler for configuration reload", extra={"component": "config"})
        except AttributeError:
            # Windows may not support SIGHUP; log at debug level only.
            self.logger.debug("SIGHUP not supported on this platform", extra={"component": "config"})

    def _read_config_file(self, path: str) -> Dict[str, Any]:
        if not os.path.exists(path):
            raise FileNotFoundError(f"Config file not found: {path}")
        with open(path, "r", encoding="utf-8") as stream:
            data = yaml.safe_load(stream) or {}
            if not isinstance(data, dict):
                raise ValueError("Top-level configuration must be a mapping")
            return data

    @property
    def last_loaded_at(self) -> Optional[datetime]:
        return self._last_loaded_at


def load_config(config_path: str, logger: Optional[logging.Logger] = None) -> AppConfig:
    """Helper to load configuration once."""
    manager = ConfigManager(config_path=config_path, logger=logger)
    return manager.config
