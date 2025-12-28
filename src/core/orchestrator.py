"""Main system orchestrator (Phase 1 bootstrap).

This orchestrator wires configuration, logging, database, and Telegram.
"""
from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime
from typing import Optional
import os
import yaml

from src.database.operations import create_engine_from_config, init_db, DatabaseSessionManager
from src.database.models import Post, PostStatus, Account
from src.core.scheduler import Scheduler
from src.platforms.reddit_adapter import RedditAdapter
from src.core.state_manager import StateManager
from src.telegram.controller import TelegramController
from src.utils.config_loader import ConfigManager
from src.utils.logger import setup_logger
from src.content.templates import TemplateManager
from src.content.generator import ContentGenerator


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

            self._load_content_resources()
            self._register_signal_handlers()

            self._load_persisted_state()
            self._schedule_recurring_tasks()

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
        """Handle auto-posting of approved content if configured."""
        try:
            with self.db.session_scope() as session:
                post = session.query(Post).filter(Post.id == post_id).first()
                if not post or post.status != PostStatus.APPROVED:
                    self.logger.warning(f"Post {post_id} not found or not approved, skipping auto-post")
                    return

                # Check if auto-posting is enabled for this platform
                platform_config = self.config_manager.get(f"platforms.{post.platform}")
                if not platform_config.get('auto_post_after_approval', False):
                    self.logger.info(f"Auto-posting disabled for {post.platform}, skipping")
                    return

                # Get account for the platform
                account = session.query(Account).filter(
                    Account.platform == post.platform,
                    Account.is_active == True  # noqa: E712
                ).first()

                if not account:
                    self.logger.error(f"No active account found for platform {post.platform}")
                    return

                # Initialize platform adapter
                adapter = None
                if post.platform == 'reddit':
                    adapter = RedditAdapter(account, self.config_manager)
                # Add other platform adapters here

                if not adapter:
                    self.logger.error(f"No adapter found for platform {post.platform}")
                    return

                # Post the content
                self.logger.info(f"Auto-posting content to {post.platform}...")
                result = await adapter.post_comment(
                    content=post.content,
                    target_url=post.target_url,
                    metadata=post.metadata
                )

                if result.success:
                    post.status = PostStatus.POSTED
                    post.posted_at = datetime.utcnow()
                    post.external_id = result.data.get('id')
                    session.add(post)
                    self.logger.info(f"Successfully posted content to {post.platform}")
                else:
                    post.status = PostStatus.FAILED
                    post.error_message = result.error
                    session.add(post)
                    self.logger.error(
                        f"Failed to post content to {post.platform}: {result.error}"
                    )

        except Exception as e:
            self.logger.error(f"Error in _handle_approved_post: {str(e)}", exc_info=True)

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

    def _schedule_recurring_tasks(self) -> None:
        # Schedule daily routine (human-in-the-loop, no auto-posting).
        # Runs once every 24h; can be triggered manually by calling execute_daily_routine.
        self.scheduler.schedule_every(
            "daily_routine",
            interval_seconds=24 * 3600,
            start_in_seconds=30,
            coro_factory=lambda: self.execute_daily_routine(),
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

    async def execute_daily_routine(self) -> None:
        """Generate drafts for configured platforms (no auto-post)."""
        if self.content_generator is None:
            self.logger.error("Content generator not initialized", extra={"component": "orchestrator"})
            return
        reddit_cfg = self.config_manager.config.platforms.reddit
        if not reddit_cfg.enabled:
            return
        drafted = []
        max_per_day = reddit_cfg.max_posts_per_day
        threshold = reddit_cfg.quality_threshold
        try:
            for idx, subreddit in enumerate(reddit_cfg.subreddits):
                if idx >= max_per_day:
                    break
                res = self.content_generator.generate_reddit_comment(subreddit=subreddit)
                quality = res.get("quality", {})
                score = quality.get("score", 0.0)
                template_id = res.get("template_id")

                with self.db.session_scope(logger=self.logger) as session:
                    post = Post(
                        account_id=None,
                        platform="reddit",
                        content=res.get("content", ""),
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
                            "errors": res.get("errors"),
                            "warnings": res.get("warnings"),
                            "subreddit": subreddit,
                        },
                    )
                    session.add(post)
                    session.flush()
                    drafted.append({"subreddit": subreddit, "score": score, "post_id": post.id})

                # Human review if below threshold
                if self.telegram and score < threshold:
                    context = {
                        "subreddit": subreddit,
                        "score": score,
                        "content": res.get("content", ""),
                        "template_id": template_id,
                        "suggestions": quality.get("suggestions"),
                    }
                    review = await self.telegram.request_human_input(
                        action_type="CONTENT_REVIEW",
                        context=context,
                        timeout=self.config_manager.config.telegram.timeout_seconds,
                    )
                    decision = "timeout" if review.timeout else "approved"
                    post_id = drafted[-1]["post_id"]
                    if review.response_value:
                        # If edited content returned
                        new_content = review.response_value
                        with self.db.session_scope(logger=self.logger) as session:
                            session.query(Post).filter(Post.id == post_id).update(
                                {
                                    "content": new_content, 
                                    "human_approved": True,
                                    "status": PostStatus.APPROVED
                                }
                            )
                    elif not review.timeout:
                        with self.db.session_scope(logger=self.logger) as session:
                            session.query(Post).filter(Post.id == post_id).update(
                                {
                                    "human_approved": True,
                                    "status": PostStatus.APPROVED
                                }
                            )
                    
                    # Handle auto-posting if enabled
                    if not review.timeout and reddit_cfg.auto_post_after_approval:
                        asyncio.create_task(self._handle_approved_post(post_id))
                    
                    self.logger.info(
                        f"Content review completed, auto-post: {reddit_cfg.auto_post_after_approval}",
                        extra={"component": "orchestrator", "subreddit": subreddit, "decision": decision},
                    )

            # Notify summary
            if self.telegram and drafted:
                lines = ["Daily routine drafts (no auto-post):"]
                for item in drafted:
                    lines.append(f"- r/{item['subreddit']}: score={item['score']:.2f}, post_id={item['post_id']}")
                await self.telegram.send_notification("\n".join(lines), priority="INFO")
        except Exception as exc:
            self.logger.error(
                "Daily routine failed",
                extra={"component": "orchestrator", "error": str(exc)},
            )
