"""Main system orchestrator (Phase 1 bootstrap).

This orchestrator wires configuration, logging, database, and Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta
from typing import Optional, Any
import os
import yaml

from src.database.operations import create_engine_from_config, init_db, DatabaseSessionManager
from src.database.models import Post, PostStatus, Account
from src.core.scheduler import Scheduler
from src.platforms.reddit_adapter import RedditAdapter
from src.platforms.youtube_adapter import YouTubeAdapter
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


class SystemOrchestrator:
    def __init__(self, config_path: str) -> None:
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
        self._stop_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        try:
            self.logger.info("Starting orchestrator", extra={"component": "orchestrator"})

            self.telegram = TelegramController(
                bot_token=self.config_manager.config.telegram.bot_token,
                user_chat_id=self.config_manager.config.telegram.user_chat_id,
                config=self.config_manager.config,
                db=self.db,
                logger=self.logger.getChild("telegram"),
                log_file_path=self.config_manager.config.logging.file_path,
            )
            self._validate_config()
            if self.health_checker:
                self.health_checker.telegram = self.telegram
            if self.alert_manager:
                self.alert_manager.telegram = self.telegram

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
        try:
            while not self._stop_event.is_set():
                # Persist state every 60 seconds.
                self._persist_state()
                await asyncio.wait_for(self._stop_event.wait(), timeout=60.0)
        except asyncio.TimeoutError:
            return await self.main_loop()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self.logger.error("Main loop error", extra={"component": "orchestrator", "error": str(exc)})

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
        
    async def _auto_post_approved_content(self, post_id: int) -> None:
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
                if not post or post.status != PostStatus.APPROVED:
                    self.logger.warning(f"Post {post_id} not found or not approved, skipping auto-post")
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
            
            # Post the content using the adapter
            self.logger.info(f"Posting content to {platform} (post_id={post_id})...")
            
            target_post = None
            account_creds: Optional[Dict[str, Any]] = None
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

                if self.account_manager:
                    acct = self.account_manager.get_best_account("reddit")
                    if not acct:
                        acct = self.account_manager.rotate_accounts("reddit")
                    if acct:
                        account_creds = self.account_manager.get_account_credentials(acct)

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

            # Post the content
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
                    # Handle failure
                    error_msg = post_result.error or "Unknown error"
                    post.status = PostStatus.FAILED
                    post.error_message = error_msg
                    session.add(post)
                    
                    # Send error notification
                    if self.telegram:
                        message = (
                            f"❌ Failed to post to {platform.upper()}\n"
                            f"📝 Post ID: {post_id}\n"
                            f"❌ Error: {error_msg}"
                        )
                        await self.telegram.send_notification(message, priority="ERROR")
                    
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
            self._stop_event.set()
            self.scheduler.stop()
            if self.telegram is not None:
                await self.telegram.send_notification("System shutting down", priority="WARNING")
                await self.telegram.stop()
            self._persist_state()
        except Exception as exc:
            self.logger.error(
                "Error during shutdown",
                extra={"component": "orchestrator", "error": str(exc)},
            )
        finally:
            self._shutdown_event.set()

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
            if os.path.exists(synonyms_path):
                with open(synonyms_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                    if isinstance(data, dict):
                        synonyms = data.get("synonyms", {})
        except Exception as exc:
            self.logger.error(
                "Failed to load synonyms",
                extra={"component": "orchestrator", "path": synonyms_path, "error": str(exc)},
            )
        self.content_generator = ContentGenerator(
            config=self.config_manager.config,
            templates=self.template_manager,
            synonyms=synonyms,
        )

    def _init_platform_adapters(self) -> None:
        cfg = self.config_manager.config
        self.reddit_adapter = None  # instantiate on demand with active DB session
        self.youtube_adapter = YouTubeAdapter(
            config=cfg,
            credentials=[],
            logger=self.logger.getChild("youtube"),
            telegram=self.telegram,
        )
        # Integrate monitoring helpers with adapters if needed later

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
            return adapter, session
        except Exception as exc:
            self.logger.error(
                "Failed to initialize Reddit adapter",
                extra={"component": "orchestrator", "error": str(exc)},
            )
            return None, None

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
        for idx, subreddit in enumerate(reddit_cfg.subreddits):
            if idx >= max_per_day:
                break
            res = self.content_generator.generate_reddit_comment(subreddit=subreddit)
            drafted.append(await self._persist_and_review(post_data=res, platform="reddit", meta={"subreddit": subreddit}, threshold=threshold))
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
        for video in videos:
            res = self.content_generator.generate_youtube_comment(video_title=video.get("title",""), video_description=video.get("description",""))
            drafted.append(await self._persist_and_review(
                post_data=res,
                platform="youtube",
                meta={"video_id": video.get("id"), "video_url": video.get("url")},
                threshold=youtube_cfg.get("quality_threshold", 0.7),
            ))
        return [d for d in drafted if d]

    async def _persist_and_review(self, post_data: Dict[str, Any], platform: str, meta: Dict[str, Any], threshold: float) -> Optional[Dict[str, Any]]:
        quality = post_data.get("quality", {})
        score = quality.get("score", 0.0)
        template_id = post_data.get("template_id")
        with self.db.session_scope(logger=self.logger) as session:
            post = Post(
                account_id=None,
                platform=platform,
                content=post_data.get("content", ""),
                url=None,
                posted_at=None,
                status=PostStatus.pending,
                clicks=0,
                conversions=0,
                quality_score=score,
                human_approved=False,
                metadata_json={
                    "template_id": template_id,
                    "quality": quality,
                    "errors": post_data.get("errors"),
                    "warnings": post_data.get("warnings"),
                    **meta,
                },
            )
            session.add(post)
            session.flush()
            post_id = post.id

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

    async def _run_health_check(self) -> None:
        if not self.health_checker:
            return
        try:
            results = await self.health_checker.check_all()
            overall = results.get("overall")
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
