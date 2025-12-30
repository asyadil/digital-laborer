"""TikTok comment adapter (stub with rate-limit/backoff and anti-bot hooks)."""
from __future__ import annotations

import importlib
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
        self.simulate = bool(cfg.get("simulate", True))
        rate = 1 / max(cfg.get("min_delay_between_comments", 60), 1)
        self.rate_limiter = TokenBucketRateLimiter(rate=rate, capacity=3)
        self.daily_limiter = TokenBucketRateLimiter(
            rate=cfg.get("max_comments_per_day", 30) / 86400.0, capacity=max(cfg.get("max_comments_per_day", 30), 1)
        )
        self.challenge_probability = float(cfg.get("challenge_probability", 0.01))
        self.rate_limit_cooldown = int(cfg.get("rate_limit_cooldown_seconds", 120))
        self._cfg_ref = getattr(getattr(config, "platforms", None), "tiktok", None)
        self._ua_pool = cfg.get("user_agents") or []
        self._proxy_pool = cfg.get("proxies") or []
        self._current_ua: Optional[str] = None
        self._current_proxy: Optional[str] = None
        self._driver = self._load_driver(cfg.get("driver_class"))

    def _rotate_identity(self) -> None:
        cfg_obj = self._cfg_ref
        if hasattr(cfg_obj, "dict"):
            cfg = cfg_obj.dict()
        elif isinstance(cfg_obj, dict):
            cfg = cfg_obj
        else:
            cfg = {}
        self._ua_pool = cfg.get("user_agents") or self._ua_pool
        self._proxy_pool = cfg.get("proxies") or self._proxy_pool
        ua, proxy = self._choose_identity(self._ua_pool, self._proxy_pool)
        self._current_ua = ua or pick_random_user_agent()
        self._current_proxy = proxy

    def _require_auth(self, account: Dict[str, Any]) -> None:
        if self.simulate:
            return
        if not account.get("auth_token") and not account.get("session_cookies"):
            raise AuthenticationError("TikTok credentials missing (auth_token/session_cookies).")

    def _post_comment_real(self, target_id: str, content: str, account: Dict[str, Any]) -> AdapterResult:
        if not self._driver:
            raise PlatformAdapterError("TikTok real posting not implemented; enable simulate or provide driver.")
        res = self._driver.post_comment(target_id=target_id, content=content, account=account)
        if res.get("success"):
            data = res.get("data", {})
            return AdapterResult(success=True, data=data)
        return AdapterResult(
            success=False,
            data=res.get("data", {"error_code": "post_failed"}),
            error=res.get("error") or "post_failed",
            retry_recommended=bool(res.get("retry_recommended", False)),
        )

    def login(self, account: Dict[str, Any]) -> AdapterResult:
        self._rotate_identity()
        if not self.simulate and self._driver:
            res = self._driver.login(account)
            if res.get("success", True):
                return AdapterResult(success=True, data=res.get("data", {}))
            return AdapterResult(success=False, data=res.get("data", {}), error=res.get("error"))
        username = account.get("username", "unknown")
        return AdapterResult(success=True, data={"username": username, "proxy": self._current_proxy})

    def find_target_posts(self, location: str, limit: int = 5) -> AdapterResult:
        if not self.simulate and self._driver:
            res = self._driver.find_target_posts(location=location, limit=limit)
            return AdapterResult(success=bool(res.get("success", True)), data=res.get("data", {}), error=res.get("error"))
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
            return AdapterResult(
                success=False,
                data={
                    "error_code": "rate_limit",
                    "backoff_seconds": self.rate_limit_cooldown,
                    "rotate_identity": True,
                },
                error=f"TikTok rate limit reached; cooldown {self.rate_limit_cooldown}s",
                retry_recommended=True,
            )
        self._rotate_identity()
        self._require_auth(account)
        # Simulate anti-bot challenge probability
        if random.random() < self.challenge_probability:
            return AdapterResult(
                success=False,
                data={
                    "error_code": "captcha_required",
                    "challenge_type": "captcha_or_verification",
                    "rotate_identity": True,
                    "backoff_seconds": 300,
                },
                error="Captcha or verification required",
                retry_recommended=False,
            )
        if not self.simulate:
            return self._post_comment_real(target_id, content, account)

        comment_id = f"ttc_{int(time.time())}_{random.randint(1000,9999)}"
        self._mark_proxy_success(self._current_proxy)
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
        if not self.simulate and self._driver:
            res = self._driver.get_comment_metrics(comment_url)
            return AdapterResult(success=bool(res.get("success", True)), data=res.get("data", {}), error=res.get("error"))
        metrics = {"likes": random.randint(0, 20), "replies": random.randint(0, 5)}
        return AdapterResult(success=True, data=metrics)

    def check_account_health(self, account: Dict[str, Any]) -> AdapterResult:
        if not self.simulate and self._driver:
            res = self._driver.check_account_health(account)
            return AdapterResult(success=bool(res.get("success", True)), data=res.get("data", {}), error=res.get("error"))
        return AdapterResult(success=True, data={"status": "healthy"})

    def close(self) -> None:
        return None

    def _load_driver(self, driver_class: Optional[str]):
        if not driver_class:
            return None
        try:
            module_name, class_name = driver_class.rsplit(".", 1)
            module = importlib.import_module(module_name)
            klass = getattr(module, class_name)
            return klass()
        except Exception as exc:
            self.logger.error("Failed to load TikTok driver %s: %s", driver_class, exc)
            return None
