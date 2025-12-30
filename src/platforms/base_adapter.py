"""Abstract base class for platform adapters."""
from __future__ import annotations

import abc
import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


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
        self._proxy_failures: Dict[str, float] = {}
        self._proxy_failure_counts: Dict[str, int] = {}
        self._rng = random.Random()

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

    def post_comment_with_backoff(
        self,
        target_id: str,
        content: str,
        account: Dict[str, Any],
        *,
        max_attempts: int = 3,
        base_delay: float = 2.0,
        rotate_identity_cb=None,
    ) -> AdapterResult:
        """Optional helper with simple backoff + rotation hook."""
        attempt = 0
        delay = base_delay
        last_error: Optional[str] = None
        while attempt < max_attempts:
            attempt += 1
            try:
                return self.post_comment(target_id, content, account)
            except RateLimitError as exc:
                last_error = str(exc)
                if rotate_identity_cb:
                    rotate_identity_cb()
                jitter = self._rng.uniform(0.85, 1.3)
                time.sleep(delay * jitter)
                delay = min(delay * 2, 60)
            except PlatformAdapterError as exc:
                return AdapterResult(
                    success=False,
                    data={"error_code": "post_failed"},
                    error=str(exc),
                    retry_recommended=False,
                )
        return AdapterResult(
            success=False,
            data={"error_code": "rate_limit"},
            error=last_error or "rate_limited",
            retry_recommended=True,
        )

    def _choose_identity(
        self,
        ua_pool: Optional[list[str]],
        proxy_pool: Optional[list[str]],
        *,
        proxy_cooldown_seconds: int = 300,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Pick UA and proxy with simple cooldown for bad proxies."""
        ua = self._rng.choice(ua_pool) if ua_pool else None
        proxy = None
        now = time.time()
        healthy = []
        if proxy_pool:
            for p in proxy_pool:
                expiry = self._proxy_failures.get(p, 0)
                if expiry and expiry > now:
                    continue
                healthy.append(p)
            pool = healthy or proxy_pool
            proxy = self._rng.choice(pool)
        return ua, proxy

    def _mark_proxy_failure(self, proxy: Optional[str], cooldown_seconds: int = 300) -> None:
        if not proxy:
            return
        current = self._proxy_failure_counts.get(proxy, 0) + 1
        self._proxy_failure_counts[proxy] = current
        backoff = min(cooldown_seconds * (2 ** (current - 1)), 3600)
        self._proxy_failures[proxy] = time.time() + max(5, backoff)

    def _mark_proxy_success(self, proxy: Optional[str]) -> None:
        if not proxy:
            return
        if proxy in self._proxy_failure_counts:
            self._proxy_failure_counts[proxy] = 0
        if proxy in self._proxy_failures:
            self._proxy_failures.pop(proxy, None)

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
