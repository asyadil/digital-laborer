"""TikTok comment adapter (stub with rate-limit/backoff and anti-bot hooks)."""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, Optional

from src.platforms.base_adapter import (
    AdapterResult,
    AntiBotChallengeError,
    AuthenticationError,
    BasePlatformAdapter,
    PlatformAdapterError,
    RateLimitError,
)
from src.utils.rate_limiter import TokenBucketRateLimiter
from src.utils.user_agents import pick_random_user_agent


class TikTokAdapter(BasePlatformAdapter):
    """Lightweight adapter for TikTok comments.

    This is a stub intended to be wired with real HTTP/API calls later. It enforces
    rate limits, supports UA/proxy rotation, and surfaces standard error codes.
    """

    def __init__(self, config: Any, logger: Optional[logging.Logger] = None, telegram: Any = None) -> None:
        super().__init__(config=config, logger=logger, telegram=telegram)
        cfg = getattr(getattr(config, "platforms", None), "tiktok", {}) or {}
        if hasattr(cfg, "dict"):
            cfg = cfg.dict()
        rate = 1 / max(float(cfg.get("min_delay_between_comments", 120)), 1.0)
        self.rate_limiter = TokenBucketRateLimiter(rate=rate, capacity=3)
        self.daily_limiter = TokenBucketRateLimiter(
            rate=cfg.get("max_comments_per_day", 30) / 86400.0, capacity=max(cfg.get("max_comments_per_day", 30), 1)
        )
        self._ua_pool = cfg.get("user_agents") or []
        self._proxy_pool = cfg.get("proxies") or []
        self._current_ua: Optional[str] = None
        self._current_proxy: Optional[str] = None

    def _rotate_identity(self) -> None:
        ua, proxy = self._choose_identity(self._ua_pool, self._proxy_pool)
        self._current_ua = ua or pick_random_user_agent()
        self._current_proxy = proxy

    def login(self, account: Dict[str, Any]) -> AdapterResult:
        # Stub: assume credentials already available; rotate identity.
        self._rotate_identity()
        username = account.get("username", "unknown")
        return AdapterResult(success=True, data={"username": username, "proxy": self._current_proxy})

    def find_target_posts(self, location: str, limit: int = 5) -> AdapterResult:
        # Stub: return dummy trending posts for a hashtag/location.
        items = [
            {"id": f"tt_{int(time.time())}_{i}", "score": random.randint(50, 200), "location": location}
            for i in range(min(limit, 5))
        ]
        return AdapterResult(success=True, data={"items": items})

    def post_comment(self, target_id: str, content: str, account: Dict[str, Any]) -> AdapterResult:
        if not target_id:
            return AdapterResult(success=False, data={"error_code": "missing_target"}, error="No target_id provided")
        # Rate limiting
        if not (self.rate_limiter.try_acquire() and self.daily_limiter.try_acquire()):
            self._mark_proxy_failure(self._current_proxy)
            raise RateLimitError("TikTok rate limit reached")
        self._rotate_identity()
        # Simulate anti-bot challenge probability
        if random.random() < 0.01:
            raise AntiBotChallengeError("Captcha or verification required")
        comment_id = f"ttc_{int(time.time())}_{random.randint(1000,9999)}"
        return AdapterResult(
            success=True,
            data={
                "comment_id": comment_id,
                "comment_url": f"https://www.tiktok.com/video/{target_id}?comment_id={comment_id}",
                "username": account.get("username", "unknown"),
                "account_id": account.get("id"),
                "rotate_account": False,
            },
        )

    def get_comment_metrics(self, comment_url: str) -> AdapterResult:
        # Stub metrics
        metrics = {"likes": random.randint(0, 20), "replies": random.randint(0, 5)}
        return AdapterResult(success=True, data=metrics)

    def check_account_health(self, account: Dict[str, Any]) -> AdapterResult:
        return AdapterResult(success=True, data={"status": "healthy"})

    def close(self) -> None:
        return None
