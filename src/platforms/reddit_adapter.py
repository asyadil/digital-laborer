"""Reddit automation adapter (official API).

Uses OAuth via the official API client (PRAW). This implementation is designed
to be fault-tolerant and testable with mocks (no real network in tests).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Union

from sqlalchemy.orm import Session

from src.database.models import Account, AccountStatus, AccountHealth
from src.platforms.base_adapter import (
    AdapterResult,
    AuthenticationError,
    BasePlatformAdapter,
    PlatformAdapterError,
    RateLimitError,
)
from src.platforms.captcha_handler import CaptchaHandler
from src.utils.crypto import credential_manager
from src.utils.rate_limiter import FixedWindowRateLimiter
from src.platforms.selenium_session import SeleniumSession, SeleniumSessionConfig
from src.utils.user_agents import pick_random_user_agent
from src.utils.retry import retry_with_exponential_backoff


class RedditAdapter(BasePlatformAdapter):
    def __init__(self, config: Any, credentials: list[Dict[str, Any]] = None, logger: Optional[logging.Logger] = None, telegram: Any = None, db_session: Optional[Session] = None) -> None:
        super().__init__(config=config, logger=logger, telegram=telegram)
        self.credentials = credentials or []
        self.db_session = db_session
        # PRAW allows fairly generous limits, but we enforce a conservative client-side limit.
        self.rate_limiter = FixedWindowRateLimiter(max_calls=55, window_seconds=60.0)
        self._client: Any = None
        self._logged_in_as: Optional[str] = None
        self._current_account_id: Optional[int] = None
        self._last_health_check: Dict[int, float] = {}  # account_id -> timestamp
        self.captcha_handler = CaptchaHandler(telegram) if telegram else None
        self._selenium: Optional[SeleniumSession] = None
        reddit_cfg = getattr(getattr(config, "platforms", None), "reddit", None)
        self._proxy_pool: List[str] = list(getattr(reddit_cfg, "proxies", []) or [])
        self._ua_pool: List[str] = list(getattr(reddit_cfg, "user_agents", []) or [])
        self._current_proxy: Optional[str] = None
        self._current_ua: Optional[str] = None
        self._ops_timing: Dict[str, float] = {}
        self._jitter_range_ms = (400, 1200)
        self._max_timeout_seconds = getattr(getattr(config, "retry", None), "timeout_seconds", 30)

    def _load_account_from_db(self, account_id: int) -> Optional[Dict[str, Any]]:
        """Load and decrypt account credentials from database."""
        if not self.db_session:
            self.logger.error("Database session not provided")
            return None
            
        account = self.db_session.query(Account).filter(Account.id == account_id).first()
        if not account:
            return None
            
        # Create account dict with decrypted password
        return {
            "id": account.id,
            "platform": account.platform,
            "username": account.username,
            "password": credential_manager.decrypt(account.password_encrypted) if account.password_encrypted else None,
            "status": account.status,
            "health_score": account.health_score,
            "last_used": account.last_used,
            "metadata": account.metadata_json or {}
        }

    def _get_best_account(self) -> Optional[Dict[str, Any]]:
        """Deprecated: use AccountManager for account selection."""
        self.logger.warning("Deprecated _get_best_account called; delegate to AccountManager in orchestrator.")
        return None

    def _update_account_health(self, account_id: int, success: bool, error: str = None):
        """Update account health based on operation result."""
        if not self.db_session:
            return
            
        try:
            account = self.db_session.query(Account).filter(Account.id == account_id).first()
            if not account:
                return
                
            # Update last used time
            account.last_used = datetime.utcnow()
            
            # Update health score
            if success:
                # Gradually increase health score on success (capped at 1.0)
                account.health_score = min(1.0, (account.health_score or 0.7) + 0.05)
            else:
                # Decrease health score on failure
                account.health_score = max(0.0, (account.health_score or 1.0) - 0.2)
                
                # If health is too low, mark as flagged
                if account.health_score < 0.3:
                    account.status = AccountStatus.flagged
                    self.logger.warning(f"Account {account.username} flagged due to low health score")
            
            # Log health event
            health_event = AccountHealth(
                account_id=account_id,
                health_score=account.health_score,
                success=success,
                error=error,
                timestamp=datetime.utcnow()
            )
            self.db_session.add(health_event)
            self.db_session.commit()
            
        except Exception as e:
            self.logger.error(f"Error updating account health: {str(e)}")
            self.db_session.rollback()

    def login(self, account: Union[Dict[str, Any], int]) -> AdapterResult:
        """Login with either an account dict or account_id from database."""
        try:
            # If account is an ID, load from database
            if isinstance(account, int):
                account = self._load_account_from_db(account)
                if not account:
                    return AdapterResult(
                        success=False, 
                        error=f"Account {account} not found in database",
                        retry_recommended=False
                    )
            
            # Adapter no longer auto-picks accounts; if None, fall back to first credentials entry for backward compat/tests
            if account is None and self.credentials:
                account = self.credentials[0]
            if account is None:
                return AdapterResult(
                    success=False,
                    error="No Reddit account supplied",
                    retry_recommended=False,
                    data={},
                )
            
            # Store account ID for health updates
            self._current_account_id = account.get('id')
            self._human_jitter()
            
            # Create and test client
            try:
                self._client = self._create_client(account)
                me = self._call_with_limits(lambda: self._client.user.me())
            except AuthenticationError as auth_exc:
                if "2fa" in str(auth_exc).lower() or "otp" in str(auth_exc).lower():
                    if not self.captcha_handler:
                        raise
                    code_result = self.captcha_handler.handle_2fa_sync(platform="reddit", method="app", timeout=300)
                    if not code_result.solved or not code_result.response:
                        raise
                    self._client = self._create_client(account, otp=code_result.response)
                    me = self._call_with_limits(lambda: self._client.user.me())
                else:
                    raise
            
            if me is None:
                error = "Reddit auth failed: user.me returned None"
                if self._current_account_id:
                    self._update_account_health(self._current_account_id, False, error)
                raise AuthenticationError(error)
                
            self._logged_in_as = getattr(me, "name", None)
            
            # Update account health on successful login
            if self._current_account_id:
                self._update_account_health(self._current_account_id, True)
            
            return AdapterResult(
                success=True, 
                data={
                    "username": self._logged_in_as,
                    "account_id": self._current_account_id
                }
            )
            
        except AuthenticationError as exc:
            if self._current_account_id:
                self._update_account_health(self._current_account_id, False, str(exc))
            shot = self._capture_screenshot_safe("login_error")
            return AdapterResult(
                success=False, 
                error=str(exc), 
                retry_recommended=False,
                data={"screenshot": shot} if shot else {},
            )
            
        except (RateLimitError, PlatformAdapterError) as exc:
            if self._current_account_id:
                self._update_account_health(self._current_account_id, False, str(exc))
            shot = self._capture_screenshot_safe("login_ratelimit")
            return AdapterResult(
                success=False, 
                error=str(exc), 
                retry_recommended=True,
                data={"screenshot": shot} if shot else {},
            )
            
        except Exception as exc:
            if self._current_account_id:
                self._update_account_health(self._current_account_id, False, str(exc))
            self.logger.error(f"Unexpected error in Reddit login: {str(exc)}", exc_info=True)
            shot = self._capture_screenshot_safe("login_unexpected")
            return AdapterResult(
                success=False, 
                error=f"Unexpected error: {str(exc)}", 
                retry_recommended=True,
                data={"screenshot": shot} if shot else {},
            )

    def find_target_posts(self, location: str, limit: int = 10, min_score: int = 10, min_comments: int = 0, account: Optional[Union[Dict[str, Any], int]] = None) -> AdapterResult:
        """
        Find target posts in a subreddit (location=subreddit).
        
        Args:
            location: Subreddit name (with or without r/)
            limit: Maximum number of posts to return
            min_score: Minimum score (upvotes) for a post to be included
            min_comments: Minimum number of comments for a post to be included
            
        Returns:
            AdapterResult with list of posts and metadata
        """
        try:
            # Ensure we're logged in with supplied account
            if self._client is None:
                login_result = self.login(account)
                if not login_result.success:
                    return login_result
            self._human_jitter()
            
            subreddit_name = self._normalize_subreddit(location)
            
            try:
                def _op():
                    sub = self._client.subreddit(subreddit_name)
                    items = []
                    # Get more posts than requested to filter by score/comments
                    fetch_limit = max(limit * 3, 50)
                    for submission in sub.hot(limit=fetch_limit):
                        self._human_scroll()
                        try:
                            score = getattr(submission, "score", 0)
                            num_comments = getattr(submission, "num_comments", 0)
                            
                            # Skip if post doesn't meet criteria
                            if score < min_score or num_comments < min_comments:
                                continue
                                
                            items.append({
                                "id": getattr(submission, "id", None),
                                "title": getattr(submission, "title", ""),
                                "url": getattr(submission, "url", ""),
                                "permalink": "https://www.reddit.com" + getattr(submission, "permalink", ""),
                                "score": score,
                                "created_utc": getattr(submission, "created_utc", None),
                                "num_comments": num_comments,
                                "subreddit": subreddit_name,
                                "author": getattr(submission, "author", None),
                                "is_self": getattr(submission, "is_self", False),
                                "over_18": getattr(submission, "over_18", False),
                                "stickied": getattr(submission, "stickied", False),
                            })
                            
                            # Stop if we have enough posts
                            if len(items) >= limit:
                                break
                                
                        except Exception as e:
                            self.logger.warning(f"Error processing post: {str(e)}")
                            continue
                            
                    return items

                start = time.monotonic()
                posts = self._call_with_limits(_op)
                self._log_duration("find_target_posts", start)
                
                # Update account health on success
                if self._current_account_id:
                    self._update_account_health(self._current_account_id, True)
                
                return AdapterResult(
                    success=True, 
                    data={
                        "items": posts, 
                        "subreddit": subreddit_name,
                        "account_id": self._current_account_id,
                        "duration_ms": round((time.monotonic() - start) * 1000, 2),
                    }
                )
                
            except Exception as e:
                error_msg = f"Error finding posts in r/{subreddit_name}: {str(e)}"
                self.logger.error(error_msg, exc_info=True)
                shot = self._capture_screenshot_safe("find_posts_error")
                
                # Update account health on failure
                if self._current_account_id:
                    self._update_account_health(self._current_account_id, False, error_msg)
                
                return AdapterResult(
                    success=False, 
                    error=error_msg, 
                    retry_recommended=not isinstance(e, AuthenticationError),
                    data={"screenshot": shot} if shot else {},
                )
                
        except Exception as exc:
            error_msg = f"Unexpected error in find_target_posts: {str(exc)}"
            self.logger.error(error_msg, exc_info=True)
            return AdapterResult(
                success=False, 
                error=error_msg, 
                retry_recommended=not isinstance(exc, AuthenticationError)
            )

    def post_comment(self, target_id: str, content: str, account: Optional[Union[Dict[str, Any], int]] = None) -> AdapterResult:
        """
        Post a comment to a Reddit submission.
        
        Args:
            target_id: The Reddit submission ID to comment on
            content: The comment content
            account: Either an account dict, account ID, or None to use best available
            
        Returns:
            AdapterResult with comment details on success
        """
        try:
            # Ensure we have a valid client with supplied account
            if self._client is None or (account and isinstance(account, int) and account != self._current_account_id):
                login_result = self.login(account)
                if not login_result.success:
                    return login_result
            self._human_jitter()
            
            if not target_id:
                raise ValueError("target_id is required")
                
            if not content or not content.strip():
                raise ValueError("content is empty")
            
            # Ensure content isn't too long (Reddit limit is 10,000 chars)
            if len(content) > 9500:
                content = content[:9497] + "..."
            
            try:
                def _op():
                    submission = self._client.submission(id=target_id)
                    
                    # Check if we've already commented on this post
                    if self._current_account_id:
                        for comment in submission.comments.list():
                            if hasattr(comment, 'author') and comment.author == self._logged_in_as:
                                return {
                                    "comment_id": getattr(comment, "id", None),
                                    "comment_url": "https://www.reddit.com" + getattr(comment, "permalink", ""),
                                    "duplicate": True
                                }
                    
                    # Post the comment with a human-like pause
                    self._human_jitter()
                    self._human_scroll()
                    comment = submission.reply(content)
                    return {
                        "comment_id": getattr(comment, "id", None), 
                        "comment_url": "https://www.reddit.com" + getattr(comment, "permalink", ""),
                        "duplicate": False
                    }
                
                start = time.monotonic()
                res = self._call_with_limits(_op)
                self._log_duration("post_comment", start)
                
                # Update account health on success
                if self._current_account_id:
                    self._update_account_health(self._current_account_id, True)
                
                return AdapterResult(
                    success=True, 
                    data={
                        **res,
                        "account_id": self._current_account_id,
                        "username": self._logged_in_as,
                        "duration_ms": round((time.monotonic() - start) * 1000, 2),
                    }
                )
                
            except Exception as e:
                error_msg = f"Error posting comment: {str(e)}"
                self.logger.error(error_msg, exc_info=True)
                
                # Update account health on failure
                if self._current_account_id:
                    self._update_account_health(self._current_account_id, False, error_msg)
                
                # If we got rate limited, signal retry/rotate to caller
                if "RATELIMIT" in str(e).upper():
                    shot = self._capture_screenshot_safe("ratelimit")
                    return AdapterResult(
                        success=False,
                        error=error_msg,
                        retry_recommended=True,
                        data={
                            "rotate_account": True,
                            "account_id": self._current_account_id,
                            "screenshot": shot,
                            "duration_ms": round((time.monotonic() - start) * 1000, 2),
                        },
                    )
                
                return AdapterResult(
                    success=False, 
                    error=error_msg, 
                    retry_recommended=not isinstance(e, AuthenticationError)
                )
                
        except Exception as exc:
            error_msg = f"Unexpected error in post_comment: {str(exc)}"
            self.logger.error(error_msg, exc_info=True)
            return AdapterResult(
                success=False, 
                error=error_msg, 
                retry_recommended=not isinstance(exc, AuthenticationError)
            )

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
            shadow = self.check_shadowban(account)
            data["shadowban"] = shadow
            if shadow.get("shadowbanned") and shadow.get("confidence", 0) > 0.8:
                data["issues"].append("shadowban_detected")
                data["health_score"] = min(data["health_score"], 0.3)
                if self.db_session and account.get("id"):
                    db_acc = self.db_session.query(Account).filter(Account.id == account.get("id")).first()
                    if db_acc:
                        db_acc.status = AccountStatus.flagged
                        db_acc.health_score = min(db_acc.health_score or 1.0, shadow.get("confidence", 0.3))
                        self.db_session.add(db_acc)
                        self.db_session.commit()
                if self.telegram:
                    try:
                        asyncio.create_task(
                            self.telegram.send_notification(
                                f"⚠️ Reddit shadowban detected for {account.get('username','?')} (confidence {shadow.get('confidence'):.2f})",
                                priority="ERROR",
                            )
                        )
                    except Exception:
                        pass
            return AdapterResult(success=True, data=data)
        except Exception as exc:
            return AdapterResult(success=False, data={}, error=str(exc), retry_recommended=True)

    def check_shadowban(self, account: Dict[str, Any]) -> Dict[str, Any]:
        """Detect potential shadowban using multiple heuristics."""
        results: Dict[str, Any] = {"shadowbanned": False, "confidence": 0.0, "reasons": [], "methods_used": {}}
        confidences: List[float] = []

        # Method A: r/ShadowBan test post
        try:
            sub = self._client.subreddit("ShadowBan")
            submission = sub.submit(
                title="Am I shadowbanned?",
                selftext="Automated shadowban check.",
                send_replies=False,
            )
            time.sleep(5)
            submission.refresh()
            comments = list(getattr(submission, "comments", []) or [])
            response_text = " ".join([getattr(c, "body", "").lower() for c in comments])
            method_conf = 0.0
            if "shadowbanned" in response_text:
                method_conf = 0.95
                results["reasons"].append("AutoModerator indicates shadowban")
            elif "not shadowbanned" in response_text:
                method_conf = 0.05
            self._human_jitter()
            self._human_scroll()
        except Exception as exc:
            self.logger.warning("Shadowban test post failed", extra={"error": str(exc)})
            results["methods_used"]["shadowban_post"] = {"error": str(exc), "confidence": 0.0}

        # Method B: last 5 comments visibility
            user = self._client.user.me()
            comments = list(user.comments.new(limit=5))
            visible = 0
            total = 0
            for c in comments:
                total += 1
                try:
                    parent = c.submission
                    parent.comments.replace_more(limit=0)
                    found = any(getattr(child, "id", "") == getattr(c, "id", "") for child in parent.comments.list())
                    if found:
                        visible += 1
                except Exception:
                    continue
            visibility_rate = (visible / total) if total else 1.0
            conf_b = 1.0 - visibility_rate
            confidences.append(conf_b)
            results["methods_used"]["comment_visibility"] = {
                "visible": visible,
                "total": total,
                "visibility_rate": visibility_rate,
                "confidence": conf_b,
            }
            if conf_b > 0.5:
                results["reasons"].append("Low visibility for recent comments")
        except Exception as exc:
            self.logger.warning("Shadowban comment visibility check failed", extra={"error": str(exc)})
            results["methods_used"]["comment_visibility"] = {"error": str(exc), "confidence": 0.0}

        # Method C: post history engagement
        try:
            user = self._client.user.me()
            subs = list(user.submissions.new(limit=20))
            scores = [int(getattr(s, "score", 0)) for s in subs]
            low_engagement = sum(1 for s in scores if s <= 1)
            ratio = (low_engagement / len(scores)) if scores else 0.0
            avg_score = sum(scores) / len(scores) if scores else 0.0
            conf_c = 0.0
            if ratio > 0.7:
                conf_c = 0.6
                results["reasons"].append("Most posts have ≤1 score")
            if avg_score < 1 and len(scores) >= 5:
                conf_c = max(conf_c, 0.5)
            confidences.append(conf_c)
            results["methods_used"]["history_analysis"] = {
                "average_score": avg_score,
                "low_score_ratio": ratio,
                "confidence": conf_c,
            }
        except Exception as exc:
            self.logger.warning("Shadowban history analysis failed", extra={"error": str(exc)})
            results["methods_used"]["history_analysis"] = {"error": str(exc), "confidence": 0.0}

        # Aggregate confidence
        if confidences:
            agg = sum(confidences) / len(confidences)
        else:
            agg = 0.0
        results["confidence"] = round(agg, 3)
        results["shadowbanned"] = agg >= 0.5
        return results

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

    def _create_client(self, account: Dict[str, Any], otp: Optional[str] = None) -> Any:
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
        user_agent_cfg = oauth_cfg.get("user_agent")
        username = oauth_cfg.get("username")
        password = oauth_cfg.get("password")
        if not all([client_id, client_secret, user_agent, username, password]):
            raise AuthenticationError("Missing Reddit OAuth credentials")

        if otp:
            password = f"{password}:{otp}"

        self._rotate_identity(preferred_ua=user_agent_cfg)

        requestor_kwargs = {}
        if self._current_proxy:
            requestor_kwargs = {
                "proxies": {
                    "http": self._current_proxy,
                    "https": self._current_proxy,
                }
            }
            self.logger.info("Using proxy for Reddit client", extra={"proxy": self._current_proxy})

        self.logger.info(
            "Using user agent for Reddit client",
            extra={"ua": self._current_ua},
        )

        return praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=self._current_ua,
            username=username,
            password=password,
            requestor_kwargs=requestor_kwargs or None,
        )

    def _rotate_identity(self, preferred_ua: Optional[str] = None) -> None:
        """Rotate user-agent/proxy for this session."""
        self._current_ua = preferred_ua or pick_random_user_agent(self._ua_pool) or "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        self._current_proxy = random.choice(self._proxy_pool) if self._proxy_pool else None

    def _ensure_selenium(self) -> None:
        """Lazily create Selenium session for screenshots/anti-bot diagnostics."""
        if self._selenium:
            return
        cfg = SeleniumSessionConfig(
            headless=True,
            proxy=self._current_proxy,
            user_agent=self._current_ua,
        )
        self._selenium = SeleniumSession(config=cfg, logger=self.logger.getChild("selenium"))
        try:
            self._selenium.start()
            self._selenium.human_pause(600, 1400)
        except Exception:
            self._selenium = None

    def _capture_screenshot_safe(self, label: str) -> Optional[str]:
        """Capture screenshot if Selenium session is available/initializable."""
        if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("DISABLE_SELENIUM_SCREENSHOT"):
            return None
        try:
            self._ensure_selenium()
            if not self._selenium or not self._selenium.driver:
                return None
            target = f"{label}_{int(time.time())}.png"
            return self._selenium.capture_screenshot(target)
        except Exception:
            return None

    def _human_scroll(self) -> None:
        """Small scroll to mimic human activity."""
        if not self._selenium or not self._selenium.driver:
            return
        try:
            self._selenium.driver.execute_script("window.scrollBy(0, arguments[0]);", random.randint(200, 800))
            self._selenium.human_pause(400, 1200)
        except Exception:
            pass

    def _human_jitter(self) -> None:
        """Random short pause to mimic human interaction."""
        time.sleep(random.uniform(self._jitter_range_ms[0] / 1000.0, self._jitter_range_ms[1] / 1000.0))

    def _log_duration(self, name: str, start_time: float) -> None:
        try:
            elapsed = round((time.monotonic() - start_time) * 1000, 2)
            self.logger.debug("Adapter timing", extra={"component": "reddit_adapter", "operation": name, "duration_ms": elapsed})
        except Exception:
            pass

    @retry_with_exponential_backoff(max_attempts=3, base_delay=1.0, max_delay=15.0)
    def _call_with_limits(self, func):
        # Client-side rate limiter
        if not self.rate_limiter.try_acquire():
            raise RateLimitError("Client-side rate limit exceeded")
        return func()
