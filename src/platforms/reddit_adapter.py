"""Reddit automation adapter (official API).

Uses OAuth via the official API client (PRAW). This implementation is designed
to be fault-tolerant and testable with mocks (no real network in tests).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, Optional

from src.platforms.base_adapter import (
    AdapterResult,
    AuthenticationError,
    BasePlatformAdapter,
    PlatformAdapterError,
    RateLimitError,
)
from src.utils.rate_limiter import FixedWindowRateLimiter
from src.utils.retry import retry_with_exponential_backoff


class RedditAdapter(BasePlatformAdapter):
    def __init__(self, config: Any, credentials: list[Dict[str, Any]], logger: Optional[logging.Logger] = None, telegram: Any = None) -> None:
        super().__init__(config=config, logger=logger, telegram=telegram)
        self.credentials = credentials
        # PRAW allows fairly generous limits, but we enforce a conservative client-side limit.
        self.rate_limiter = FixedWindowRateLimiter(max_calls=55, window_seconds=60.0)
        self._client: Any = None
        self._logged_in_as: Optional[str] = None

    def login(self, account: Dict[str, Any]) -> AdapterResult:
        try:
            self._client = self._create_client(account)
            # Validate credentials by calling /api/v1/me
            me = self._call_with_limits(lambda: self._client.user.me())
            if me is None:
                raise AuthenticationError("Reddit auth failed: user.me returned None")
            self._logged_in_as = getattr(me, "name", None)
            return AdapterResult(success=True, data={"username": self._logged_in_as})
        except AuthenticationError as exc:
            return AdapterResult(success=False, data={}, error=str(exc), retry_recommended=False)
        except RateLimitError as exc:
            return AdapterResult(success=False, data={}, error=str(exc), retry_recommended=True)
        except PlatformAdapterError as exc:
            return AdapterResult(success=False, data={}, error=str(exc), retry_recommended=True)
        except Exception as exc:
            return AdapterResult(success=False, data={}, error=str(exc), retry_recommended=True)

    def find_target_posts(self, location: str, limit: int = 10) -> AdapterResult:
        """Find target posts in a subreddit (location=subreddit)."""
        try:
            if self._client is None:
                raise AuthenticationError("Not logged in")
            subreddit_name = self._normalize_subreddit(location)

            def _op():
                sub = self._client.subreddit(subreddit_name)
                items = []
                for submission in sub.hot(limit=max(1, min(limit, 50))):
                    items.append(
                        {
                            "id": getattr(submission, "id", None),
                            "title": getattr(submission, "title", ""),
                            "url": getattr(submission, "url", ""),
                            "permalink": "https://www.reddit.com" + getattr(submission, "permalink", ""),
                            "score": getattr(submission, "score", 0),
                            "created_utc": getattr(submission, "created_utc", None),
                            "num_comments": getattr(submission, "num_comments", 0),
                        }
                    )
                return items

            posts = self._call_with_limits(_op)
            return AdapterResult(success=True, data={"items": posts, "subreddit": subreddit_name})
        except Exception as exc:
            return AdapterResult(success=False, data={}, error=str(exc), retry_recommended=True)

    def post_comment(self, target_id: str, content: str, account: Dict[str, Any]) -> AdapterResult:
        try:
            if self._client is None:
                raise AuthenticationError("Not logged in")
            if not target_id:
                raise ValueError("target_id is required")
            if not content or not content.strip():
                raise ValueError("content is empty")

            def _op():
                submission = self._client.submission(id=target_id)
                comment = submission.reply(content)
                comment_url = "https://www.reddit.com" + getattr(comment, "permalink", "")
                return {"comment_id": getattr(comment, "id", None), "comment_url": comment_url}

            res = self._call_with_limits(_op)
            return AdapterResult(success=True, data=res)
        except Exception as exc:
            return AdapterResult(success=False, data={}, error=str(exc), retry_recommended=True)

    def get_comment_metrics(self, comment_url: str) -> AdapterResult:
        try:
            if self._client is None:
                raise AuthenticationError("Not logged in")
            comment_id = self._parse_comment_id(comment_url)
            if not comment_id:
                raise ValueError("Unable to parse comment id")

            def _op():
                comment = self._client.comment(id=comment_id)
                comment.refresh()
                return {
                    "score": getattr(comment, "score", 0),
                    "replies": len(getattr(comment, "replies", []) or []),
                    "created_utc": getattr(comment, "created_utc", None),
                }

            metrics = self._call_with_limits(_op)
            return AdapterResult(success=True, data=metrics)
        except Exception as exc:
            return AdapterResult(success=False, data={}, error=str(exc), retry_recommended=True)

    def check_account_health(self, account: Dict[str, Any]) -> AdapterResult:
        try:
            if self._client is None:
                raise AuthenticationError("Not logged in")

            def _op():
                me = self._client.user.me()
                if me is None:
                    raise AuthenticationError("user.me returned None")
                created = getattr(me, "created_utc", None)
                created_at = datetime.utcfromtimestamp(created) if isinstance(created, (int, float)) else None
                link_karma = getattr(me, "link_karma", 0)
                comment_karma = getattr(me, "comment_karma", 0)
                total_karma = int(link_karma) + int(comment_karma)
                # Simple heuristic health score.
                health_score = 0.4
                issues = []
                if total_karma >= 100:
                    health_score += 0.3
                if total_karma >= 500:
                    health_score += 0.2
                if created_at and (datetime.utcnow() - created_at).days >= 30:
                    health_score += 0.1
                return {
                    "health_score": max(0.0, min(1.0, health_score)),
                    "issues": issues,
                    "total_karma": total_karma,
                    "created_at": created_at.isoformat() if created_at else None,
                }

            data = self._call_with_limits(_op)
            return AdapterResult(success=True, data=data)
        except Exception as exc:
            return AdapterResult(success=False, data={}, error=str(exc), retry_recommended=True)

    def close(self) -> None:
        self._client = None
        self._logged_in_as = None

    def _normalize_subreddit(self, location: str) -> str:
        s = (location or "").strip()
        if s.lower().startswith("r/"):
            s = s[2:]
        return s

    def _parse_comment_id(self, url: str) -> Optional[str]:
        if not url:
            return None
        # Common pattern: .../comments/<post_id>/<slug>/<comment_id>/
        m = re.search(r"/comments/\w+/[^/]+/(\w+)/?", url)
        if m:
            return m.group(1)
        # Alternate: comment id in query or fragment is not handled.
        return None

    def _create_client(self, account: Dict[str, Any]) -> Any:
        try:
            import praw  # type: ignore
        except Exception as exc:
            raise PlatformAdapterError(f"praw is required for RedditAdapter: {exc}")

        oauth = getattr(getattr(self.config, "platforms", None), "reddit", None)
        oauth_cfg = getattr(oauth, "oauth", {}) if oauth is not None else {}
        # Allow per-account override.
        oauth_cfg = {**(oauth_cfg or {}), **(account.get("oauth", {}) if isinstance(account, dict) else {})}

        client_id = oauth_cfg.get("client_id")
        client_secret = oauth_cfg.get("client_secret")
        user_agent = oauth_cfg.get("user_agent")
        username = oauth_cfg.get("username")
        password = oauth_cfg.get("password")
        if not all([client_id, client_secret, user_agent, username, password]):
            raise AuthenticationError("Missing Reddit OAuth credentials")

        return praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent,
            username=username,
            password=password,
        )

    @retry_with_exponential_backoff(max_attempts=3, base_delay=1.0, max_delay=15.0)
    def _call_with_limits(self, func):
        # Client-side rate limiter
        if not self.rate_limiter.try_acquire():
            raise RateLimitError("Client-side rate limit exceeded")
        return func()
