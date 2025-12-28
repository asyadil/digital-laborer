"""Reddit automation adapter (official API).

Uses OAuth via the official API client (PRAW). This implementation is designed
to be fault-tolerant and testable with mocks (no real network in tests).
"""
from __future__ import annotations

import logging
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
from src.utils.crypto import credential_manager
from src.utils.rate_limiter import FixedWindowRateLimiter
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
        """Get the best available account based on health score and last used time."""
        if not self.db_session:
            self.logger.error("Database session not provided")
            return None
            
        # Find the healthiest, least recently used active account
        account = (self.db_session.query(Account)
                  .filter(
                      Account.platform == 'reddit',
                      Account.status == AccountStatus.active,
                      Account.health_score > 0.5  # Only use accounts with good health
                  )
                  .order_by(
                      Account.health_score.desc(),
                      Account.last_used.asc()  # Prefer least recently used
                  )
                  .first())
                  
        if not account:
            self.logger.error("No healthy Reddit accounts available")
            return None
            
        return self._load_account_from_db(account.id)

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
            
            # If no account provided, try to find the best available
            if not account and self.db_session:
                account = self._get_best_account()
                if not account:
                    return AdapterResult(
                        success=False,
                        error="No healthy Reddit accounts available",
                        retry_recommended=False
                    )
            
            # If we still don't have an account, use the first credential
            if not account and self.credentials:
                account = self.credentials[0]
            
            if not account:
                return AdapterResult(
                    success=False,
                    error="No Reddit accounts available",
                    retry_recommended=False
                )
            
            # Store account ID for health updates
            self._current_account_id = account.get('id')
            
            # Create and test client
            self._client = self._create_client(account)
            me = self._call_with_limits(lambda: self._client.user.me())
            
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
            return AdapterResult(
                success=False, 
                error=str(exc), 
                retry_recommended=False
            )
            
        except (RateLimitError, PlatformAdapterError) as exc:
            if self._current_account_id:
                self._update_account_health(self._current_account_id, False, str(exc))
            return AdapterResult(
                success=False, 
                error=str(exc), 
                retry_recommended=True
            )
            
        except Exception as exc:
            if self._current_account_id:
                self._update_account_health(self._current_account_id, False, str(exc))
            self.logger.error(f"Unexpected error in Reddit login: {str(exc)}", exc_info=True)
            return AdapterResult(
                success=False, 
                error=f"Unexpected error: {str(exc)}", 
                retry_recommended=True
            )

    def find_target_posts(self, location: str, limit: int = 10, min_score: int = 10, min_comments: int = 5) -> AdapterResult:
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
            # Ensure we're logged in
            if self._client is None:
                login_result = self.login(None)  # Try to login with best available account
                if not login_result.success:
                    return login_result
            
            subreddit_name = self._normalize_subreddit(location)
            
            try:
                def _op():
                    sub = self._client.subreddit(subreddit_name)
                    items = []
                    # Get more posts than requested to filter by score/comments
                    fetch_limit = max(limit * 3, 50)
                    for submission in sub.hot(limit=fetch_limit):
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

                posts = self._call_with_limits(_op)
                
                # Update account health on success
                if self._current_account_id:
                    self._update_account_health(self._current_account_id, True)
                
                return AdapterResult(
                    success=True, 
                    data={
                        "items": posts, 
                        "subreddit": subreddit_name,
                        "account_id": self._current_account_id
                    }
                )
                
            except Exception as e:
                error_msg = f"Error finding posts in r/{subreddit_name}: {str(e)}"
                self.logger.error(error_msg, exc_info=True)
                
                # Update account health on failure
                if self._current_account_id:
                    self._update_account_health(self._current_account_id, False, error_msg)
                
                return AdapterResult(
                    success=False, 
                    error=error_msg, 
                    retry_recommended=not isinstance(e, AuthenticationError)
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
            # Ensure we have a valid client
            if self._client is None or (account and isinstance(account, int) and account != self._current_account_id):
                login_result = self.login(account)
                if not login_result.success:
                    return login_result
            
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
                    
                    # Post the comment
                    comment = submission.reply(content)
                    return {
                        "comment_id": getattr(comment, "id", None), 
                        "comment_url": "https://www.reddit.com" + getattr(comment, "permalink", ""),
                        "duplicate": False
                    }
                
                res = self._call_with_limits(_op)
                
                # Update account health on success
                if self._current_account_id:
                    self._update_account_health(self._current_account_id, True)
                
                return AdapterResult(
                    success=True, 
                    data={
                        **res,
                        "account_id": self._current_account_id,
                        "username": self._logged_in_as
                    }
                )
                
            except Exception as e:
                error_msg = f"Error posting comment: {str(e)}"
                self.logger.error(error_msg, exc_info=True)
                
                # Update account health on failure
                if self._current_account_id:
                    self._update_account_health(self._current_account_id, False, error_msg)
                
                # If we got rate limited, try with a different account
                if "RATELIMIT" in str(e).upper() and self.db_session:
                    self.logger.warning("Rate limited, trying with a different account...")
                    return self.post_comment(target_id, content, None)  # Let it pick a new account
                
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
