"""Abstract base class for platform adapters."""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional


class PlatformAdapterError(RuntimeError):
    """Base error for platform adapter failures."""


class AuthenticationError(PlatformAdapterError):
    pass


class RateLimitError(PlatformAdapterError):
    pass


class AntiBotChallengeError(PlatformAdapterError):
    """Captcha/2FA/verification challenges requiring human input."""


class PostFailedError(PlatformAdapterError):
    pass


@dataclass(frozen=True)
class AdapterResult:
    success: bool
    data: Dict[str, Any]
    error: Optional[str] = None
    retry_recommended: bool = False


class BasePlatformAdapter(abc.ABC):
    """Base adapter contract.

    Each adapter must be failure-isolated and must not crash the orchestrator.
    """

    def __init__(self, config: Any, logger: Optional[logging.Logger] = None, telegram: Any = None) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.telegram = telegram

    @abc.abstractmethod
    def login(self, account: Dict[str, Any]) -> AdapterResult:
        raise NotImplementedError

    @abc.abstractmethod
    def find_target_posts(self, location: str, limit: int = 10) -> AdapterResult:
        """Find target posts/threads for a given location (e.g., subreddit/topic).

        Returns AdapterResult.data containing a list of post objects and relevance scores.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def post_comment(self, target_id: str, content: str, account: Dict[str, Any]) -> AdapterResult:
        """Post a comment/reply to a target post/thread."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_comment_metrics(self, comment_url: str) -> AdapterResult:
        """Fetch engagement metrics for a posted comment."""
        raise NotImplementedError

    @abc.abstractmethod
    def check_account_health(self, account: Dict[str, Any]) -> AdapterResult:
        raise NotImplementedError

    @abc.abstractmethod
    def close(self) -> None:
        raise NotImplementedError
