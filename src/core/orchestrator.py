"""Main system orchestrator (Phase 1 bootstrap).

This orchestrator wires configuration, logging, database, and Telegram.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Any, List, Dict

import yaml

from src.telegram.playbooks import build_playbook
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.database.operations import create_engine_from_config, init_db, DatabaseSessionManager
from src.database.models import Post, PostStatus, Account, ReferralLink
from src.database.migrations import run_migrations
from src.core.scheduler import Scheduler
from src.platforms.reddit_adapter import RedditAdapter
from src.platforms.youtube_adapter import YouTubeAdapter
from src.platforms.tiktok_adapter import TikTokAdapter
from src.platforms.instagram_adapter import InstagramAdapter
from src.platforms.facebook_adapter import FacebookAdapter
from src.core.state_manager import StateManager
from src.telegram.controller import TelegramController
from src.utils.config_loader import ConfigManager
from src.utils.logger import setup_logger
from src.content.templates import TemplateManager
from src.content.generator import ContentGenerator
from src.monitoring.health_checker import HealthChecker
from src.monitoring.analytics import Analytics
from src.monitoring.alert_manager import AlertManager
from src.core.account_manager import AccountManager
from src.core.startup_validation import run_preflight_checks
from src.core.health_checker_startup import run_startup_health_checks, StartupHealthError
from src.utils.secrets_manager import SecretsManager
from src.utils.rate_limiter import TokenBucketRateLimiter


class NullTelegram:
    """Degraded-mode Telegram placeholder; logs locally only."""

    def __init__(self, logger: logging.Logger) -> None:
        self.logger = logger
        self.paused = False

    async def start(self) -> None:  # pragma: no cover - simple stub
        self.logger.warning("Telegram disabled; running in degraded mode", extra={"component": "telegram"})

    async def stop(self) -> None:  # pragma: no cover - simple stub
        return None

    async def send_notification(self, message: str, priority: str = "INFO", **_: Any) -> None:
        self.logger.log(
            logging.WARNING if priority.upper() in {"ERROR", "WARNING"} else logging.INFO,
            "[TELEGRAM DEGRADED] %s",
            message,
            extra={"component": "telegram"},
        )

    async def request_human_input(self, *_, **__) -> Any:
        class _Response:
            response_value: Optional[str] = None
            timeout: bool = True

        return _Response()


class SystemOrchestrator:
    CRITICAL_SERVICES = {"database"}
    OPTIONAL_SERVICES = {"telegram", "reddit", "youtube", "monitoring"}

    def __init__(self, config_path: str, skip_validation: bool = False) -> None:
        self.base_path = Path(os.getenv("APP_BASE_PATH", Path.cwd()))
        self.skip_validation = skip_validation
        self.secrets = SecretsManager(env_file_path=self.base_path / ".env")
        self._hydrate_env_secrets()
        self.config_manager = ConfigManager(config_path=config_path)
        self.config_manager.register_reload_on_sighup()

        self.logger = setup_logger(
            name="referral_system",
            level=self.config_manager.config.logging.level,
            log_file=self.config_manager.config.logging.file_path,
            log_format=self.config_manager.config.logging.format,
            max_file_size_mb=self.config_manager.config.logging.max_file_size_mb,
            backup_count=self.config_manager.config.logging.backup_count,
        )

        engine = create_engine_from_config(self.config_manager.config.database)
        init_db(engine)
        run_migrations(engine)
        self.db = DatabaseSessionManager(engine=engine)

        self.state_manager = StateManager(db=self.db, logger=self.logger.getChild("state"))
        self.scheduler = Scheduler(logger=self.logger.getChild("scheduler"))

        self.telegram: Optional[TelegramController] = None
        self.template_manager: Optional[TemplateManager] = None
        self.content_generator: Optional[ContentGenerator] = None
        self.health_checker: Optional[HealthChecker] = HealthChecker(db=self.db, telegram=None, logger=self.logger.getChild("health"))
        self.analytics: Optional[Analytics] = Analytics(db=self.db, logger=self.logger.getChild("analytics"))
        self.alert_manager: Optional[AlertManager] = AlertManager(telegram=None, logger=self.logger.getChild("alert"))
        self.account_manager: Optional[AccountManager] = AccountManager(db=self.db, logger=self.logger.getChild("accounts"))
        self.reddit_adapter: Optional[RedditAdapter] = None
        self.youtube_adapter: Optional[YouTubeAdapter] = None
        self.tiktok_adapter: Optional[TikTokAdapter] = None
        self.instagram_adapter: Optional[InstagramAdapter] = None
        self.facebook_adapter: Optional[FacebookAdapter] = None
        self._stop_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self.service_health: dict[str, str] = {}
        self.degraded_mode = False
        self._recovery_backoff_seconds = 300
        self._monitoring_disabled = False
        self._fallback_referral_links: List[Dict[str, Any]] = []
        # Basic per-platform rate limiters (tokens per second, capacity burst)
        self.platform_limiters: dict[str, TokenBucketRateLimiter] = {
            "reddit": TokenBucketRateLimiter(rate=1 / 30, capacity=5),   # ~2 per minute burst 5
            "youtube": TokenBucketRateLimiter(rate=1 / 60, capacity=3),  # ~1 per minute burst 3
            "tiktok": TokenBucketRateLimiter(rate=1 / 120, capacity=3),
            "instagram": TokenBucketRateLimiter(rate=1 / 120, capacity=3),
            "facebook": TokenBucketRateLimiter(rate=1 / 180, capacity=3),
        }
        self.platform_paused: dict[str, bool] = {}

    async def start(self) -> None:
        try:
            self.logger.info("Starting orchestrator", extra={"component": "orchestrator"})

            if not self.skip_validation:
                report = run_preflight_checks(
                    config=self.config_manager.config,
                    engine=self.db.engine,
                    base_path=self.base_path,
                )
                formatted = report.format()
                print(formatted)
                self.logger.info(
                    "Pre-flight checks completed",
                    extra={
                        "component": "orchestrator",
                        "errors": len(report.errors),
                        "warnings": len(report.warnings),
                    },
                )
                if report.errors:
                    raise SystemExit("Startup blocked due to pre-flight errors")
            else:
                self.logger.warning(
                    "Pre-flight validation skipped via flag --skip-validation",
                    extra={"component": "orchestrator"},
                )

            self._init_telegram()
            self._validate_config()
            if self.health_checker:
                self.health_checker.telegram = self.telegram
            if self.alert_manager:
                self.alert_manager.telegram = self.telegram
            try:
                await run_startup_health_checks(
                    engine=self.db.engine,
                    telegram=self.telegram,
                    scheduler=self.scheduler,
                    logger=self.logger,
                    critical=["database"],
                )
            except StartupHealthError as exc:
                self.logger.error(
                    "Startup health checks failed",
                    extra={"component": "orchestrator", "error": str(exc)},
                )
                raise SystemExit(str(exc))

            self._load_content_resources()
            self._register_signal_handlers()

            self._load_persisted_state()
            self._schedule_recurring_tasks()

            self._init_platform_adapters()

            telegram_task = asyncio.create_task(self.telegram.start(), name="telegram")
            scheduler_task = asyncio.create_task(self.scheduler.run(self._stop_event), name="scheduler")
            main_loop_task = asyncio.create_task(self.main_loop(), name="main_loop")

            done, pending = await asyncio.wait(
                {telegram_task, scheduler_task, main_loop_task},
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for t in done:
                exc = t.exception()
                if exc is not None:
                    raise exc
            for t in pending:
                t.cancel()
        except Exception as exc:
            self.logger.exception(
                "Orchestrator start failed",
                extra={"component": "orchestrator", "error": str(exc)},
            )
            raise

    async def main_loop(self) -> None:
        """Primary event loop for periodic persistence and health checks."""
        while not self._stop_event.is_set():
            try:
                start = datetime.utcnow()
                self._persist_state()
                await asyncio.wait_for(self._stop_event.wait(), timeout=60.0)
            except asyncio.TimeoutError:
                # expected to wake up and loop again
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.error(
                    "Main loop error",
                    extra={"component": "orchestrator", "error": str(exc)},
                )
                await asyncio.sleep(1.0)
            finally:
                elapsed = (datetime.utcnow() - start).total_seconds()
                self.logger.debug(
                    "Main loop tick",
                    extra={"component": "orchestrator", "elapsed_sec": round(elapsed, 3)},
                )

    def _register_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()

        def _handler() -> None:
            loop.create_task(self.graceful_shutdown())

        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, _handler)
            except NotImplementedError:
                # Windows
                signal.signal(sig, lambda *_args: _handler())

    async def _handle_approved_post(self, post_id: int) -> None:
        """Handle auto-posting of approved content if configured.
        
        This is a compatibility wrapper for the new _auto_post_approved_content method.
        """
        return await self._auto_post_approved_content(post_id)
        
    async def _auto_post_approved_content(self, post_id: int, *, force_rotate: bool = False, manual_retry: bool = False) -> None:
        """Post approved content to the target platform.
        
        Args:
            post_id: ID of the post to publish
            
        This method handles the entire auto-posting workflow including:
        - Loading the post from the database
        - Finding a healthy account
        - Posting the content
        - Updating the post status
        - Sending notifications
        """
        try:
            # Load the post from the database
            with self.db.session_scope() as session:
                post = session.query(Post).filter(Post.id == post_id).first()
                if not post:
                    self.logger.warning(f"Post {post_id} not found, skipping auto-post")
                    return False
                if manual_retry and post.status in {PostStatus.FAILED, PostStatus.APPROVED}:
                    post.status = PostStatus.APPROVED
                    meta = post.metadata_json or {}
                    meta.pop("blocked_auto", None)
                    meta.pop("blocked_reason", None)
                    meta.pop("skip_auto", None)
                    post.metadata_json = meta
                    session.add(post)
                    session.commit()
                if post.status != PostStatus.APPROVED:
                    self.logger.warning(f"Post {post_id} status={post.status} not eligible for auto-post")
                    return False
                meta = post.metadata_json or {}
                if meta.get("skip_auto"):
                    self.logger.info(f"Post {post_id} marked skip_auto; skipping post attempt")
                    return False
                
                platform = post.platform

                # Check if auto-posting is enabled for this platform
                platform_config = getattr(self.config_manager.config.platforms, platform, None)
                if not platform_config or not getattr(platform_config, "auto_post_after_approval", False):
                    self.logger.info(f"Auto-posting disabled for {platform}, skipping")
                    return False

                # Update post status to POSTING
                post.status = PostStatus.POSTING
                post.updated_at = datetime.utcnow()
                session.add(post)
                session.commit()

            # Initialize the appropriate adapter
            adapter = None
            adapter_session = None
            if platform == "reddit":
                adapter, adapter_session = self._get_reddit_adapter()
            elif platform == "tiktok":
                adapter = self.tiktok_adapter
            elif platform == "instagram":
                adapter = self.instagram_adapter
            elif platform == "facebook":
                adapter = self.facebook_adapter
            if not adapter:
                error_msg = f"No adapter found for platform {platform}"
                self.logger.error(error_msg)
                with self.db.session_scope() as session:
                    post = session.merge(post)
                    post.status = PostStatus.FAILED
                    post.error_message = error_msg
                    session.add(post)
                if adapter_session:
                    adapter_session.close()
                return False
            
            def _select_account() -> Optional[Dict[str, Any]]:
                if self.account_manager:
                    acct = None
                    if force_rotate:
                        acct = self.account_manager.rotate_accounts(platform)
                    else:
                        acct = self.account_manager.get_best_account(platform)
                        if not acct:
                            acct = self.account_manager.rotate_accounts(platform)
                    if acct:
                        return self.account_manager.get_account_credentials(acct)
                return None

            # Prepare target and credentials
            target_post = None
            account_creds: Optional[Dict[str, Any]] = _select_account()
            if platform == "reddit":
                subreddit = post.metadata_json.get("subreddit") if post.metadata_json else None
                if not subreddit:
                    error_msg = "No subreddit specified in post metadata"
                    self.logger.error(error_msg)
                    with self.db.session_scope() as session:
                        post = session.merge(post)
                        post.status = PostStatus.FAILED
                        post.error_message = error_msg
                        session.add(post)
                    return False

                find_result = adapter.find_target_posts(
                    location=subreddit,
                    limit=5,
                    min_score=10,
                    min_comments=2,
                    account=account_creds,
                )
                if not find_result.success or not find_result.data.get("items"):
                    error_msg = "No suitable target posts found"
                    self.logger.warning(error_msg)
                    with self.db.session_scope() as session:
                        post = session.merge(post)
                        post.status = PostStatus.FAILED
                        post.error_message = error_msg
                        session.add(post)
                    return False

                target_post = find_result.data["items"][0]
            elif platform in {"tiktok", "instagram", "facebook"}:
                location = post.metadata_json.get("location") if post.metadata_json else None
                if not location:
                    error_msg = "No location/target context provided"
                    self.logger.error(error_msg)
                    with self.db.session_scope() as session:
                        post = session.merge(post)
                        post.status = PostStatus.FAILED
                        post.error_message = error_msg
                        session.add(post)
                    return False
                find_result = adapter.find_target_posts(location=location, limit=3)
                if not find_result.success or not find_result.data.get("items"):
                    error_msg = "No suitable targets found"
                    self.logger.warning(error_msg)
                    with self.db.session_scope() as session:
                        post = session.merge(post)
                        post.status = PostStatus.FAILED
                        post.error_message = error_msg
                        session.add(post)
                    return False
                target_post = find_result.data["items"][0]

            # Post the content
            self.logger.info(f"Posting content to {platform} (post_id={post_id})...")
            post_result = await adapter.post_comment(
                target_id=target_post["id"] if target_post else None,
                content=post.content,
                account=account_creds,
            )
            
            # Handle the result
            with self.db.session_scope() as session:
                post = session.merge(post)
                
                if post_result.success:
                    # Update post status and metadata
                    post.status = PostStatus.POSTED
                    post.posted_at = datetime.utcnow()
                    post.external_id = post_result.data.get('comment_id')
                    post.url = post_result.data.get('comment_url')
                    
                    # Update metadata with posting details
                    metadata = post.metadata_json or {}
                    metadata.update({
                        'posted_at': post.posted_at.isoformat(),
                        'external_id': post.external_id,
                        'url': post.url,
                        'account_id': post_result.data.get('account_id'),
                        'username': post_result.data.get('username'),
                        'target_post': target_post if target_post else None
                    })
                    post.metadata_json = metadata
                    
                    session.add(post)
                    
                    # Send success notification
                    if self.telegram:
                        message = (
                            f"✅ Successfully posted to {platform.upper()}\n"
                            f"📝 Post ID: {post_id}\n"
                            f"🔗 URL: {post.url}\n"
                            f"👤 Account: {post_result.data.get('username', 'Unknown')}"
                        )
                        await self.telegram.send_notification(message, priority="INFO")
                    
                    self.logger.info(f"Successfully posted content to {platform} (post_id={post_id})")
                    return True
                else:
                    # Handle failure (with auto-mode retry if allowed)
                    error_msg = post_result.error or "Unknown error"
                    code = None
                    backoff_seconds = 0
                    rotate_flag = False
                    if isinstance(post_result.data, dict):
                        code = post_result.data.get("error_code") or code
                        backoff_seconds = int(post_result.data.get("backoff_seconds") or 0)
                        rotate_flag = bool(post_result.data.get("rotate_account") or post_result.data.get("rotate_identity"))
                    playbook = build_playbook(code)

                    # Auto-mode recovery path for auto-safe errors
                    if self.telegram and getattr(self.telegram, "auto_mode", False) and playbook.auto_safe:
                        if backoff_seconds > 0:
                            await asyncio.sleep(min(backoff_seconds, 300))
                        if rotate_flag:
                            # rotate account and retry once
                            account_creds = _select_account()
                        retry_result = await adapter.post_comment(
                            target_id=target_post["id"] if target_post else None,
                            content=post.content,
                            account=account_creds,
                        )
                        if retry_result.success:
                            post.status = PostStatus.POSTED
                            post.posted_at = datetime.utcnow()
                            post.external_id = retry_result.data.get("comment_id")
                            post.url = retry_result.data.get("comment_url")
                            metadata = post.metadata_json or {}
                            metadata.update(
                                {
                                    "posted_at": post.posted_at.isoformat(),
                                    "external_id": post.external_id,
                                    "url": post.url,
                                    "account_id": retry_result.data.get("account_id"),
                                    "username": retry_result.data.get("username"),
                                    "target_post": target_post if target_post else None,
                                }
                            )
                            post.metadata_json = metadata
                            session.add(post)
                            if self.telegram:
                                await self.telegram.send_notification(
                                    f"✅ Auto-mode retry succeeded ({platform.upper()}) post_id={post_id}",
                                    priority="INFO",
                                )
                            return True

                    # If still failing
                    post.status = PostStatus.FAILED
                    post.error_message = error_msg
                    session.add(post)

                    blocked = False
                    if self.telegram and getattr(self.telegram, "auto_mode", False) and not playbook.auto_safe:
                        blocked = True
                        post.status = PostStatus.APPROVED  # keep approved for manual retry
                        metadata = post.metadata_json or {}
                        metadata.update({"blocked_auto": True, "blocked_reason": code or "unknown"})
                        post.metadata_json = metadata
                        session.add(post)

                    # Send error notification with actionable buttons
                    if self.telegram:
                        note = "blocked_auto" if blocked else "error"
                        guidance = ""
                        if playbook and code == "captcha_required":
                            guidance = "\n".join([f"📋 {playbook.title}"] + [f"- {step}" for step in playbook.steps])
                        keyboard = InlineKeyboardMarkup(
                            [
                                [
                                    InlineKeyboardButton("🔁 Retry", callback_data=f"action:auto:retry:{post_id}"),
                                    InlineKeyboardButton(
                                        "🔄 Rotate+Retry", callback_data=f"action:auto:rotate:{post_id}"
                                    ),
                                ],
                                [InlineKeyboardButton("⏭️ Skip", callback_data=f"action:auto:skip:{post_id}")],
                            ]
                        )
                        message = (
                            f"❌ Failed to post to {platform.upper()} ({note})\n"
                            f"📝 Post ID: {post_id}\n"
                            f"❌ Error: {error_msg}"
                        )
                        if guidance:
                            message += f"\n\n{guidance}"
                        await self.telegram.send_notification(message, priority="ERROR", reply_markup=keyboard)
                    
                    self.logger.error(
                        f"Failed to post content to {platform} (post_id={post_id}): {error_msg}",
                        extra={"error": error_msg, "post_id": post_id, "platform": platform}
                    )
                    return False
            if adapter_session:
                adapter_session.close()
        except Exception as e:
            error_msg = f"Unexpected error in _auto_post_approved_content: {str(e)}"
            self.logger.error(error_msg, exc_info=True)
            
            # Update post status to failed
            with self.db.session_scope() as session:
                post = session.merge(post)
                post.status = PostStatus.FAILED
                post.error_message = error_msg
                session.add(post)
            
            # Send error notification
            if self.telegram:
                message = (
                    f"❌ Unexpected error while posting\n"
                    f"📝 Post ID: {post_id}\n"
                    f"❌ Error: {str(e)[:200]}"
                )
                await self.telegram.send_notification(message, priority="ERROR")
            
            return False

    async def graceful_shutdown(self) -> None:
        try:
            self.logger.info("Graceful shutdown requested", extra={"component": "orchestrator"})
            if self.alert_manager:
                self.alert_manager.reset_state()
            self._stop_event.set()
            await asyncio.sleep(1.0)
            self.logger.info("Graceful shutdown complete", extra={"component": "orchestrator"})
        except Exception as exc:
            self.logger.error(
                "Error during shutdown",
                extra={"component": "orchestrator", "error": str(exc)},
            )
        finally:
            self._shutdown_event.set()

    async def _auto_mode_poster(self, min_quality: float = 0.78, batch_size: int = 5) -> None:
        """When Telegram auto-mode is active, auto-approve high-quality drafts and post them."""
        try:
            manual_rotate: list[int] = []
            manual_retry: list[int] = []
            if self.telegram:
                manual_rotate = self.telegram.consume_auto_rotate()
                manual_retry = self.telegram.consume_auto_retry()

            # Process manual rotate/retry first, regardless of auto-mode flag
            for pid in manual_rotate:
                try:
                    await self._auto_post_approved_content(pid, force_rotate=True, manual_retry=True)
                except Exception as exc:
                    self.logger.error(
                        "Manual rotate+retry failed",
                        extra={"component": "orchestrator", "post_id": pid, "error": str(exc)},
                    )
            for pid in manual_retry:
                try:
                    await self._auto_post_approved_content(pid, manual_retry=True)
                except Exception as exc:
                    self.logger.error(
                        "Manual retry failed",
                        extra={"component": "orchestrator", "post_id": pid, "error": str(exc)},
                    )

            if not self.telegram or not getattr(self.telegram, "auto_mode", False):
                return
            with self.db.session_scope(logger=self.logger) as session:
                pending = (
                    session.query(Post)
                    .filter(Post.status == PostStatus.PENDING)
                    .filter(Post.human_approved.is_(False))
                    .filter(Post.quality_score >= min_quality)
                    .order_by(Post.created_at.asc())
                    .limit(batch_size)
                    .all()
                )
                ids_to_post: list[int] = []
                for post in pending:
                    post.human_approved = True
                    post.status = PostStatus.APPROVED
                    session.add(post)
                    ids_to_post.append(post.id)
                session.flush()
                session.commit()

            for pid in ids_to_post:
                try:
                    await self._auto_post_approved_content(pid)
                except Exception as exc:
                    self.logger.error(
                        "Auto-mode post failed",
                        extra={"component": "orchestrator", "post_id": pid, "error": str(exc)},
                    )
        except Exception as exc:
            self.logger.error(
                "Auto-mode poster error",
                extra={"component": "orchestrator", "error": str(exc)},
            )
            self._stop_event.set()
            self.scheduler.stop()
            if self.telegram is not None:
                await self.telegram.send_notification("System shutting down", priority="WARNING")
                await self.telegram.stop()
            self._persist_state()

    async def _check_adapter_health(self, platform: str) -> None:
        """Simple health check hook for new adapters."""
        try:
            adapter = None
            if platform == "tiktok":
                adapter = self.tiktok_adapter
            elif platform == "instagram":
                adapter = self.instagram_adapter
            elif platform == "facebook":
                adapter = self.facebook_adapter
            if not adapter:
                return
            account = None
            if self.account_manager:
                acct = self.account_manager.get_best_account(platform)
                if acct:
                    account = self.account_manager.get_account_credentials(acct)
            if not account:
                return
            res = adapter.check_account_health(account)
            if not res.success:
                self._set_health(platform, "degraded", reason=res.error or "unknown")
            else:
                self._set_health(platform, "healthy")
        except Exception as exc:
            self.logger.error(
                "Adapter health check failed",
                extra={"component": platform, "error": str(exc)},
            )
    def _state_key(self) -> str:
        return "orchestrator"

    def _load_persisted_state(self) -> None:
        snap = self.state_manager.get_state(self._state_key())
        if snap and self.telegram is not None:
            paused = bool(snap.value.get("paused", False))
            self.telegram.paused = paused
            self.logger.info("Loaded persisted state", extra={"component": "orchestrator", "paused": paused})

    def _persist_state(self) -> None:
        if self.telegram is None:
            return
        self.state_manager.set_state(
            self._state_key(),
            {
                "paused": bool(self.telegram.paused),
                "last_persisted_at": datetime.utcnow().isoformat(),
            },
        )

    def _validate_config(self) -> None:
        """Basic config validation to fail fast on missing critical fields."""
        cfg = self.config_manager.config
        errors = []
        if not getattr(cfg.telegram, "bot_token", None):
            errors.append("telegram.bot_token missing")
        if not getattr(cfg.telegram, "user_chat_id", None):
            errors.append("telegram.user_chat_id missing")
        db_cfg = getattr(cfg, "database", None)
        if not db_cfg or not getattr(db_cfg, "path", None):
            errors.append("database.path missing")
        if getattr(cfg.platforms, "reddit", None) and getattr(cfg.platforms.reddit, "enabled", False):
            oauth = getattr(cfg.platforms.reddit, "oauth", None)
            required = ["client_id", "client_secret", "user_agent", "username", "password"]
            for key in required:
                if not getattr(oauth, key, None):
                    errors.append(f"platforms.reddit.oauth.{key} missing")
        if getattr(cfg.platforms, "youtube", None) and getattr(cfg.platforms.youtube, "enabled", False):
            ykeys = ["client_id", "client_secret"]
            for key in ykeys:
                if not getattr(cfg.platforms.youtube, key, None):
                    errors.append(f"platforms.youtube.{key} missing")
        if errors:
            raise ValueError(f"Configuration errors: {', '.join(errors)}")

    def _seconds_until_utc_hour(self, hour: int) -> int:
        now = datetime.utcnow()
        target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        return int((target - now).total_seconds())

    def _schedule_recurring_tasks(self) -> None:
        # Schedule daily routine (human-in-the-loop, no auto-posting).
        # Runs once every 24h; can be triggered manually by calling execute_daily_routine.
        self.scheduler.schedule_every(
            "daily_routine",
            interval_seconds=24 * 3600,
            start_in_seconds=30,
            coro_factory=lambda: self.execute_daily_routine(),
        )

        # Health check every 5 minutes
        self.scheduler.schedule_every(
            "health_check",
            interval_seconds=300,
            start_in_seconds=15,
            coro_factory=lambda: self._run_health_check(),
        )

        # Recovery loop for degraded adapters/services
        self.scheduler.schedule_every(
            "service_recovery",
            interval_seconds=self._recovery_backoff_seconds,
            start_in_seconds=self._recovery_backoff_seconds,
            coro_factory=lambda: self._service_recovery_loop(),
        )

        # Refresh referral links from DB periodically (ensures bot updates are picked up)
        self.scheduler.schedule_every(
            "refresh_referrals",
            interval_seconds=600,
            start_in_seconds=45,
            coro_factory=lambda: self._refresh_referral_links(),
        )

        # Daily analytics at 09:00 UTC
        start_in = self._seconds_until_utc_hour(9)
        self.scheduler.schedule_every(
            "daily_analytics",
            interval_seconds=24 * 3600,
            start_in_seconds=start_in,
            coro_factory=lambda: self._send_daily_analytics(),
        )

        # Weekly performance review (every 7 days)
        self.scheduler.schedule_every(
            "weekly_review",
            interval_seconds=7 * 24 * 3600,
            start_in_seconds=start_in,
            coro_factory=lambda: self._send_daily_analytics(weekly=True),
        )

        # Auto-mode poster: auto-approve and post high-quality drafts when user idle
        self.scheduler.schedule_every(
            "auto_mode_poster",
            interval_seconds=600,
            start_in_seconds=60,
            coro_factory=lambda: self._auto_mode_poster(),
        )
        # Adapter health/check loops (stubs for future deeper checks)
        self.scheduler.schedule_every(
            "tiktok_health",
            interval_seconds=1800,
            start_in_seconds=120,
            coro_factory=lambda: self._check_adapter_health("tiktok"),
        )
        self.scheduler.schedule_every(
            "instagram_health",
            interval_seconds=1800,
            start_in_seconds=150,
            coro_factory=lambda: self._check_adapter_health("instagram"),
        )
        self.scheduler.schedule_every(
            "facebook_health",
            interval_seconds=1800,
            start_in_seconds=180,
            coro_factory=lambda: self._check_adapter_health("facebook"),
        )

    def _load_content_resources(self) -> None:
        templates_path = os.getenv("TEMPLATES_PATH", os.path.join("config", "templates.yaml"))
        synonyms_path = os.getenv("SYNONYMS_PATH", os.path.join("config", "synonyms.yaml"))
        try:
            self.template_manager = TemplateManager.from_yaml_file(templates_path)
        except Exception as exc:
            self.logger.error(
                "Failed to load templates",
                extra={"component": "orchestrator", "path": templates_path, "error": str(exc)},
            )
            self.template_manager = TemplateManager([])

        synonyms: dict = {}
        try:
            syn_paths: list[str] = []
            env_paths = os.getenv("SYNONYMS_PATHS")
            if env_paths:
                syn_paths = [p.strip() for p in env_paths.split(",") if p.strip()]
            else:
                primary = os.getenv("SYNONYMS_PATH", os.path.join("config", "synonyms.yaml"))
                syn_paths.append(primary)
                id_path = os.getenv("SYNONYMS_PATH_ID", os.path.join("config", "synonyms_id.yaml"))
                if id_path not in syn_paths:
                    syn_paths.append(id_path)

            for spath in syn_paths:
                if not os.path.exists(spath):
                    continue
                with open(spath, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                    if isinstance(data, dict):
                        synonyms_section = data.get("synonyms", {})
                        if isinstance(synonyms_section, dict):
                            synonyms.update(synonyms_section)
        except Exception as exc:
            self.logger.error(
                "Failed to load synonyms",
                extra={"component": "orchestrator", "path": synonyms_path, "error": str(exc)},
            )
        referral_path = os.getenv("REFERRAL_LINKS_PATH", os.path.join("config", "referral_links.yaml"))
        referral_links = {}
        try:
            referral_links = self.config_manager.load_referral_links(referral_path)
        except Exception as exc:
            self.logger.error(
                "Failed to load referral links",
                extra={"component": "orchestrator", "path": referral_path, "error": str(exc)},
            )

        self._fallback_referral_links = referral_links.get("referral_links", []) if isinstance(referral_links, dict) else []
        db_referral_links = self._load_referral_links_from_db()
        self.content_generator = ContentGenerator(
            config=self.config_manager.config,
            templates=self.template_manager,
            synonyms=synonyms,
            referral_links=db_referral_links or self._fallback_referral_links,
        )

    def _load_referral_links_from_db(self) -> List[Dict[str, Any]]:
        """Fetch active referral links from DB for content generator."""
        try:
            with self.db.session_scope(logger=self.logger) as session:
                rows: List[ReferralLink] = (
                    session.query(ReferralLink)
                    .filter(ReferralLink.active.is_(True))
                    .order_by(ReferralLink.id.desc())
                    .limit(200)
                    .all()
                )
            links: List[Dict[str, Any]] = []
            for row in rows:
                links.append(
                    {
                        "platform_name": row.platform_name,
                        "url": row.url,
                        "category": row.category,
                        "commission_rate": row.commission_rate,
                        "active": row.active,
                        # locale not stored in DB schema; defaulting to en
                        "locale": "en",
                        "clicks": row.clicks,
                        "conversions": row.conversions,
                        "earnings": row.earnings,
                    }
                )
            return links
        except Exception as exc:  # pragma: no cover - defensive guard
            self.logger.error(
                "Failed to load referral links from DB",
                extra={"component": "orchestrator", "error": str(exc)},
            )
            return []

    async def _refresh_referral_links(self) -> None:
        """Periodically refresh referral links from DB (fallback to YAML/env)."""
        if not self.content_generator:
            return
        db_links = self._load_referral_links_from_db()
        chosen = db_links or self._fallback_referral_links
        self.content_generator.referral_links = chosen
        self.logger.info(
            "Referral links refreshed",
            extra={"component": "orchestrator", "source": "db" if db_links else "fallback", "count": len(chosen)},
        )

    def _init_platform_adapters(self) -> None:
        # Shared rate limiter refs for orchestrator-level limits
        shared_limiters = self.platform_limiters
        self.reddit_adapter = None  # instantiate on demand with active DB session
        try:
            self.youtube_adapter = YouTubeAdapter(
                config=self.config_manager.config,
                credentials=[],
                logger=self.logger.getChild("youtube"),
                telegram=self.telegram,
            )
            self._set_health("youtube", "healthy")
        except Exception as exc:
            self.logger.error(
                "Failed to init YouTube adapter",
                extra={"component": "orchestrator", "error": str(exc)},
            )
            self.youtube_adapter = None
        # TikTok
        try:
            self.tiktok_adapter = TikTokAdapter(
                config=self.config_manager.config,
                logger=self.logger.getChild("tiktok"),
                telegram=self.telegram,
            )
            self._set_health("tiktok", "healthy")
        except Exception as exc:
            self.logger.error(
                "Failed to init TikTok adapter",
                extra={"component": "orchestrator", "error": str(exc)},
            )
            self.tiktok_adapter = None
        # Instagram
        try:
            self.instagram_adapter = InstagramAdapter(
                config=self.config_manager.config,
                logger=self.logger.getChild("instagram"),
                telegram=self.telegram,
            )
            self._set_health("instagram", "healthy")
        except Exception as exc:
            self.logger.error(
                "Failed to init Instagram adapter",
                extra={"component": "orchestrator", "error": str(exc)},
            )
            self.instagram_adapter = None
        # Facebook
        try:
            self.facebook_adapter = FacebookAdapter(
                config=self.config_manager.config,
                logger=self.logger.getChild("facebook"),
                telegram=self.telegram,
            )
            self._set_health("facebook", "healthy")
        except Exception as exc:
            self.logger.error(
                "Failed to init Facebook adapter",
                extra={"component": "orchestrator", "error": str(exc)},
            )
            self.facebook_adapter = None

    def _on_network_change(self, platform: str) -> None:
        """Hot-reload adapter identity pools after /netid updates."""
        platform = (platform or "").lower()
        if platform == "tiktok" and self.tiktok_adapter:
            try:
                self.tiktok_adapter._rotate_identity()
                self.logger.info("Refreshed TikTok identity pool", extra={"component": "orchestrator"})
            except Exception as exc:
                self.logger.error("Failed to refresh TikTok identity pool", extra={"component": "orchestrator", "error": str(exc)})
        elif platform == "instagram" and self.instagram_adapter:
            try:
                self.instagram_adapter._rotate_identity()
                self.logger.info("Refreshed Instagram identity pool", extra={"component": "orchestrator"})
            except Exception as exc:
                self.logger.error(
                    "Failed to refresh Instagram identity pool",
                    extra={"component": "orchestrator", "error": str(exc)},
                )
        elif platform == "facebook" and self.facebook_adapter:
            try:
                self.facebook_adapter._rotate_identity()
                self.logger.info("Refreshed Facebook identity pool", extra={"component": "orchestrator"})
            except Exception as exc:
                self.logger.error("Failed to refresh Facebook identity pool", extra={"component": "orchestrator", "error": str(exc)})
        elif platform == "youtube" and self.youtube_adapter and hasattr(self.youtube_adapter, "reload_network"):
            try:
                self.youtube_adapter.reload_network()
                self.logger.info("Refreshed YouTube network identity", extra={"component": "orchestrator"})
            except Exception as exc:
                self.logger.error("Failed to refresh YouTube network identity", extra={"component": "orchestrator", "error": str(exc)})

    def _get_reddit_adapter(self) -> tuple[Optional[RedditAdapter], Optional[object]]:
        """Create a RedditAdapter with a dedicated session for posting/health checks."""
        try:
            session = self.db._session_factory()  # type: ignore[attr-defined]
            adapter = RedditAdapter(
                config=self.config_manager.config,
                credentials=[],
                logger=self.logger.getChild("reddit"),
                telegram=self.telegram,
                db_session=session,
            )
            self._set_health("reddit", "healthy")
            return adapter, session
        except Exception as exc:
            self.logger.error(
                "Failed to initialize Reddit adapter",
                extra={"component": "orchestrator", "error": str(exc)},
            )
            self._set_health("reddit", "degraded", reason=str(exc))
            return None, None

    def _init_telegram(self) -> None:
        """Initialize Telegram with degraded fallback."""
        try:
            self.telegram = TelegramController(
                bot_token=self.config_manager.config.telegram.bot_token,
                user_chat_id=self.config_manager.config.telegram.user_chat_id,
                config=self.config_manager.config,
                db=self.db,
                logger=self.logger.getChild("telegram"),
                log_file_path=self.config_manager.config.logging.file_path,
                on_network_change=self._on_network_change,
            )
            self._set_health("telegram", "healthy")
        except Exception as exc:
            self.logger.error(
                "Telegram initialization failed; entering degraded mode",
                extra={"component": "orchestrator", "error": str(exc)},
            )
            self.degraded_mode = True
            self._set_health("telegram", "degraded")
            self.telegram = NullTelegram(logger=self.logger.getChild("telegram"))

    def _recover_adapters(self) -> None:
        """Attempt recovery for degraded optional services."""
        # Retry Reddit adapter if previously failed
        if self.reddit_adapter is None:
            adapter, _session = self._get_reddit_adapter()
            if adapter:
                self.reddit_adapter = adapter
                self._set_health("reddit", "healthy")
                self.logger.info("Recovered Reddit adapter", extra={"component": "orchestrator"})
        # Retry YouTube adapter
        if self.youtube_adapter is None:
            try:
                self.youtube_adapter = YouTubeAdapter(
                    config=self.config_manager.config,
                    credentials=[],
                    logger=self.logger.getChild("youtube"),
                    telegram=self.telegram,
                )
                self._set_health("youtube", "healthy")
                self.logger.info("Recovered YouTube adapter", extra={"component": "orchestrator"})
            except Exception as exc:
                self._set_health("youtube", "degraded", reason=str(exc))

    def _set_health(self, service: str, status: str, reason: str | None = None) -> None:
        """Track service health and optionally mark degraded mode."""
        self.service_health[service] = status
        if status != "healthy":
            self.degraded_mode = True
            if reason:
                self.logger.warning(
                    "Service %s degraded: %s", service, reason, extra={"component": "orchestrator"}
                )
            else:
                self.logger.warning(
                    "Service %s degraded", service, extra={"component": "orchestrator"}
                )
            if service in {"reddit", "youtube"}:
                self.platform_paused[service] = True
        else:
            if service in {"reddit", "youtube"}:
                self.platform_paused[service] = False
            return

    def _hydrate_env_secrets(self) -> None:
        """Populate environment variables from secrets manager if missing/placeholder."""
        required = [
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_USER_CHAT_ID",
            "REDDIT_CLIENT_ID",
            "REDDIT_CLIENT_SECRET",
            "REDDIT_USERNAME",
            "REDDIT_PASSWORD",
            "REDDIT_USER_AGENT",
            "ENCRYPTION_KEY",
            "YOUTUBE_CLIENT_ID",
            "YOUTUBE_CLIENT_SECRET",
            "YOUTUBE_REFRESH_TOKEN",
            "DATABASE_URL",
        ]

        def _is_placeholder(val: str | None) -> bool:
            if val is None:
                return True
            cleaned = val.strip()
            if not cleaned:
                return True
            lowered = cleaned.lower()
            return cleaned in {"REPLACE_ME", "CHANGE_ME", "TODO", "xxx", "XXXX"} or lowered in {"changeme", "replace_me", "xxx"}

        for name in required:
            current = os.getenv(name)
            if current and not _is_placeholder(current):
                continue
            try:
                secret = self.secrets.get(name, required=False)
                if secret and not _is_placeholder(secret):
                    os.environ[name] = secret
            except Exception:
                # Do not fail here; validation step will catch missing secrets.
                continue

    async def execute_daily_routine(self) -> None:
        """Generate drafts for configured platforms (no auto-post)."""
        if self.content_generator is None:
            self.logger.error("Content generator not initialized", extra={"component": "orchestrator"})
            return
        reddit_cfg = self.config_manager.config.platforms.reddit
        youtube_cfg = self.config_manager.config.platforms.youtube
        drafted = []
        try:
            if reddit_cfg.enabled:
                drafted.extend(await self._generate_reddit_drafts(reddit_cfg))
            if youtube_cfg.enabled:
                drafted.extend(await self._generate_youtube_pipeline(youtube_cfg))

            if self.telegram and drafted:
                lines = ["Daily routine drafts:"]
                for item in drafted:
                    lines.append(f"- {item['platform']}: score={item['score']:.2f}, post_id={item['post_id']}")
                await self.telegram.send_notification("\n".join(lines), priority="INFO")
        except Exception as exc:
            self.logger.error("Daily routine failed", extra={"component": "orchestrator", "error": str(exc)})

    async def _generate_reddit_drafts(self, reddit_cfg) -> List[Dict[str, Any]]:
        drafted = []
        max_per_day = reddit_cfg.max_posts_per_day
        threshold = reddit_cfg.quality_threshold
        locales = self._locales_to_run()
        for locale in locales:
            for idx, subreddit in enumerate(reddit_cfg.subreddits):
                if idx >= max_per_day:
                    break
                res = self.content_generator.generate_reddit_comment(subreddit=subreddit, locale=locale)
                drafted.append(
                    await self._persist_and_review(
                        post_data=res,
                        platform="reddit",
                        meta={"subreddit": subreddit, "locale": locale},
                        threshold=threshold,
                    )
                )
        return [d for d in drafted if d]

    async def _generate_youtube_pipeline(self, youtube_cfg) -> List[Dict[str, Any]]:
        drafted = []
        keywords = youtube_cfg.search_keywords or []
        if not keywords or not self.youtube_adapter:
            return drafted
        search = self.youtube_adapter.search_videos_by_keywords(keywords=keywords, limit=10)
        if not search.success:
            self.logger.warning("YouTube search failed", extra={"error": search.error})
            return drafted
        videos = search.data.get("items", [])[: youtube_cfg.max_comments_per_day]
        locales = self._locales_to_run()
        for locale in locales:
            for video in videos:
                res = self.content_generator.generate_youtube_comment(
                    video_title=video.get("title", ""),
                    video_description=video.get("description", ""),
                    locale=locale,
                )
                drafted.append(
                    await self._persist_and_review(
                        post_data=res,
                        platform="youtube",
                        meta={"video_id": video.get("id"), "video_url": video.get("url"), "locale": locale},
                        threshold=getattr(youtube_cfg, "quality_threshold", 0.7),
                    )
                )
        return [d for d in drafted if d]

    async def _persist_and_review(self, post_data: Dict[str, Any], platform: str, meta: Dict[str, Any], threshold: float) -> Optional[Dict[str, Any]]:
        quality = post_data.get("quality", {})
        score = quality.get("score", 0.0)
        template_id = post_data.get("template_id")
        limiter = self.platform_limiters.get(platform)
        warnings = post_data.get("warnings") or []
        if self.platform_paused.get(platform):
            self.logger.warning(
                "Platform %s paused due to degraded health; skipping draft",
                platform,
                extra={"component": "orchestrator", "platform": platform},
            )
            return None
        if limiter and not limiter.try_acquire():
            self.logger.warning(
                "Rate limit hit for platform %s; skipping draft persistence this tick",
                platform,
                extra={"component": "orchestrator", "platform": platform},
            )
            return None
        with self.db.session_scope(logger=self.logger) as session:
            post = Post(
                account_id=None,
                platform=platform,
                content=post_data.get("content", ""),
                url=None,
                posted_at=None,
                status=PostStatus.PENDING,
                clicks=0,
                conversions=0,
                quality_score=score,
                quality_breakdown=quality.get("breakdown"),
                human_approved=False,
                metadata_json={
                    "template_id": template_id,
                    "quality": quality,
                    "errors": post_data.get("errors"),
                    "warnings": warnings,
                    **meta,
                },
            )
            session.add(post)
            session.flush()
            post_id = post.id

        # Alert path for novelty/referral issues
        if self.telegram and warnings:
            critical_flags = [w for w in warnings if "duplicate_content_recent" in w or "alert_missing_referral_link" in w]
            if critical_flags:
                preview = (post_data.get("content") or "")[:400]
                await self.telegram.send_notification(
                    f"⚠️ Draft warning {platform.upper()}: {', '.join(critical_flags)}\nScore={score:.2f}\nPreview:\n{preview}",
                    priority="WARN",
                )

        if self.telegram and score < threshold:
            context = {
                "platform": platform,
                "score": score,
                "content": post_data.get("content", ""),
                "template_id": template_id,
                "suggestions": quality.get("suggestions"),
                **meta,
            }
            review = await self.telegram.request_human_input(
                action_type="CONTENT_REVIEW",
                context=context,
                timeout=self.config_manager.config.telegram.timeout_seconds,
            )
            if review.response_value:
                new_content = review.response_value
                with self.db.session_scope(logger=self.logger) as session:
                    session.query(Post).filter(Post.id == post_id).update(
                        {"content": new_content, "human_approved": True, "status": PostStatus.APPROVED}
                    )
            elif not review.timeout:
                with self.db.session_scope(logger=self.logger) as session:
                    session.query(Post).filter(Post.id == post_id).update(
                        {"human_approved": True, "status": PostStatus.APPROVED}
                    )
        return {"platform": platform, "score": score, "post_id": post_id}

    def _locales_to_run(self) -> List[str]:
        """Return locales for generation (default + optional parallel locales)."""
        cfg = self.config_manager.config.content
        locales = [cfg.default_locale.lower().strip()]
        for loc in getattr(cfg, "locales_parallel", []) or []:
            norm = loc.lower().strip()
            if norm and norm not in locales:
                locales.append(norm)
        return locales

    async def _run_health_check(self) -> None:
        if not self.health_checker:
            return
        try:
            results = await self.health_checker.check_all()
            overall = results.get("overall")
            for name, res in results.items():
                if name == "overall":
                    continue
                self._set_health(name, res.status, reason=res.error)
            # Critical check: database must remain healthy
            db_res = results.get("database")
            if db_res and db_res.status != "healthy":
                self.logger.critical(
                    "Database health is %s; initiating graceful shutdown",
                    db_res.status,
                    extra={"component": "orchestrator", "db_status": db_res.status, "details": db_res.details},
                )
                self._stop_event.set()
            if overall:
                self.logger.info(
                    "Health check completed",
                    extra={"component": "health", "overall_score": overall.score, "status": overall.status},
                )
                if overall.status != "healthy" and self.telegram:
                    msg = (
                        f"⚕️ Health check: {overall.status.upper()} (score={overall.score:.2f})\n"
                        + "\n".join([f"- {k}: {v.status} ({v.score:.2f})" for k, v in results.items() if k != "overall"])
                    )
                    await self.telegram.send_notification(msg, priority="WARNING")
            if self.alert_manager:
                await self.alert_manager.process_health_results(results)
        except Exception as exc:
            self.logger.error(
                "Health check failed",
                extra={"component": "health", "error": str(exc)},
            )
            self._monitoring_disabled = True
            self._set_health("monitoring", "degraded", reason=str(exc))
        finally:
            if self.account_manager:
                # Reactivate or disable accounts based on health drift
                self.account_manager.disable_unhealthy_accounts(threshold=0.25)
                self.account_manager.reactivate_recovered_accounts()

    async def _send_daily_analytics(self, weekly: bool = False) -> None:
        if not self.analytics or not self.telegram:
            return
        try:
            now = datetime.utcnow()
            if weekly:
                start = now - timedelta(days=7)
                title = "📈 Weekly Performance Review"
            else:
                start = now - timedelta(days=1)
                title = "📊 Daily Analytics"
            metrics = self.analytics.get_metrics(start, now)

            report = self._format_daily_report(title, start, now, metrics)

            await self.telegram.send_notification(report["text"], priority="INFO", reply_markup=report["buttons"])
        except Exception as exc:
            self.logger.error(
                "Daily analytics failed",
                extra={"component": "analytics", "error": str(exc)},
            )

    def _format_daily_report(self, title: str, start: datetime, end: datetime, metrics: Any) -> Dict[str, Any]:
        """Create rich ASCII report with tables and recommendations."""
        total_posts = max(1, metrics.total_posts)

        # Platform breakdown bar chart
        breakdown_lines = []
        for platform, count in metrics.posts_by_platform.items():
            pct = (count / total_posts) * 100
            bars = "█" * max(1, int(pct / 5))
            breakdown_lines.append(f"{platform.ljust(8)} | {bars.ljust(20)} {pct:5.1f}% ({count})")

        # Performance table
        perf_table = [
            "┌──────────────────────────────┐",
            f"│ Posts        : {metrics.total_posts:5d}           │",
            f"│ Clicks       : {metrics.total_clicks:5d}           │",
            f"│ Conversions  : {metrics.total_conversions:5d}      │",
            f"│ Conv. Rate   : {metrics.conversion_rate:6.2%}       │",
            f"│ Avg Quality  : {metrics.avg_quality_score:6.2f}      │",
            "└──────────────────────────────┘",
        ]

        # Top posts list
        top_lines = []
        for p in metrics.top_performing_posts:
            trend = "↑" if p.get("conversions", 0) > 0 else "→"
            top_lines.append(
                f"{trend} {p['platform']} id={p['id']} clicks={p['clicks']} conv={p['conversions']} q={p.get('quality_score')}"
            )

        # Account performance
        account_lines = []
        for platform, data in (metrics.account_performance or {}).items():
            status_icon = "✅" if data.get("avg_health_score", 0) >= 0.7 else ("⚠️" if data.get("avg_health_score",0) >= 0.4 else "❌")
            account_lines.append(
                f"{status_icon} {platform}: active={data.get('active_accounts',0)}, health={data.get('avg_health_score',0):.2f}"
            )

        # Recommendations
        recs = []
        if metrics.conversion_rate < 0.01:
            recs.append("🎯 Improve targeting or adjust referral placement.")
        if metrics.avg_quality_score < 0.7:
            recs.append("📝 Tweak templates/synonyms; raise quality threshold.")
        low_health = [p for p, d in (metrics.account_performance or {}).items() if d.get("avg_health_score",0) < 0.5]
        if low_health:
            recs.append(f"🔁 Rotate or rest accounts: {', '.join(low_health)}.")
        if not recs:
            recs.append("✅ Keep current strategy; metrics trending stable.")

        text = "\n".join(
            [
                f"{title} ({start.date()} → {end.date()})",
                *perf_table,
                "Platform breakdown:",
                *breakdown_lines,
                "",
                "Top posts:",
                *(top_lines or ["- none"]),
                "",
                "Accounts:",
                *(account_lines or ["- none"]),
                "",
                "Recommendations:",
                *recs,
            ]
        )

        # Interactive buttons
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            buttons = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("View Detailed Stats", callback_data="logs"),
                        InlineKeyboardButton("Run Health Check", callback_data="status"),
                    ],
                    [
                        InlineKeyboardButton("Review Flagged Accounts", callback_data="accounts"),
                    ],
                ]
            )
        except Exception:
            buttons = None

        return {"text": text, "buttons": buttons}
