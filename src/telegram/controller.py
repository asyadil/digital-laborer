"""Telegram bot controller with human-in-the-loop action handling."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from asyncio import Queue, QueueEmpty
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Union
import itertools
from pathlib import Path
from src.telegram.playbooks import build_playbook

from telegram import (
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    ReplyKeyboardMarkup, 
    ReplyKeyboardRemove, 
    Update
)
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest, 
    ChatMigrated, 
    NetworkError, 
    RetryAfter, 
    TelegramError, 
    TimedOut
)
from telegram.ext import (
    Application, 
    ApplicationBuilder, 
    CallbackQueryHandler, 
    CommandHandler, 
    ContextTypes, 
    MessageHandler, 
    filters
)

from src.database.models import Account, AccountType, TelegramInteraction, Post, PostStatus
from src.database.operations import DatabaseSessionManager
from src.telegram import handlers
from src.utils.rate_limiter import FixedWindowRateLimiter
from src.utils.validators import sanitize_markdown, validate_email
from src.monitoring.audit import AuditLogger


@dataclass
class HumanInputResult:
    action_id: str
    action_type: str
    response_value: Optional[str]
    responded_at: Optional[datetime]
    response_time_seconds: Optional[float]
    timeout: bool


@dataclass(order=True)
class _NotificationItem:
    priority: int
    seq: int
    message: str = field(compare=False)
    priority_label: str = field(compare=False, default="INFO")
    attachments: Optional[list[str]] = field(compare=False, default=None)
    reply_markup: Optional[Any] = field(compare=False, default=None)
    attempts: int = field(compare=False, default=0)


class TelegramController:
    """Telegram bot controller.

    Uses polling by default and persists pending actions to the database.
    """

    def __init__(
        self,
        bot_token: str,
        user_chat_id: str,
        config: Any,
        db: DatabaseSessionManager,
        logger: Optional[logging.Logger] = None,
        log_file_path: Optional[str] = None,
        on_network_change: Optional[Any] = None,
    ) -> None:
        self.bot_token = bot_token
        self.user_chat_id = str(user_chat_id)
        self.config = config
        self.db = db
        self.logger = logger or logging.getLogger("telegram")
        self.log_file_path = log_file_path
        self.on_network_change = on_network_change
        if log_file_path:
            audit_path = Path(log_file_path).with_name("audit.log")
            AuditLogger.configure(file_path=str(audit_path), logger=self.logger.getChild("audit"))

        self.started_at = datetime.utcnow()
        self.paused = False

        self._app: Optional[Application] = None
        self._send_rate_limiter = FixedWindowRateLimiter(
            max_calls=int(getattr(config.telegram, "max_messages_per_minute", 20)),
            window_seconds=60.0,
        )
        self.idle_threshold_seconds = int(getattr(getattr(config, "telegram", None), "idle_threshold_seconds", 3 * 3600))
        self.auto_mode = False
        self._last_user_activity = datetime.utcnow()
        self._notification_queue: asyncio.PriorityQueue[_NotificationItem] = asyncio.PriorityQueue()
        self._pending_futures: dict[str, asyncio.Future] = {}
        self._stop_event = asyncio.Event()
        self._notif_seq = itertools.count()
        self._background_tasks: list[asyncio.Task] = []
        self._auto_retry_queue: Queue[int] = Queue()
        self._auto_rotate_queue: Queue[int] = Queue()

    @property
    def pending_actions_count(self) -> int:
        return len(self._pending_futures)

    def notify_network_change(self, platform: str) -> None:
        """Trigger orchestrator-level callback when network identity changes."""
        if not self.on_network_change:
            return
        try:
            self.on_network_change(platform)
        except Exception as exc:
            self.logger.error(
                "Failed to propagate network change",
                extra={"component": "telegram", "platform": platform, "error": str(exc)},
            )

    async def start(self) -> None:
        """Start Telegram polling and background queue worker."""
        try:
            self._app = ApplicationBuilder().token(self.bot_token).build()
            self._register_handlers(self._app)

            # Load any pending actions from DB and rehydrate futures.
            await self._rehydrate_pending_actions()

            queue_task = asyncio.create_task(self._queue_worker(), name="telegram_queue_worker")

            await self._app.initialize()
            await self._app.start()
            await self._app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

            # Background escalation monitor
            self._background_tasks.append(
                asyncio.create_task(self._escalation_worker(), name="telegram_escalation_worker")
            )
            # Auto-mode monitor for idle user
            self._background_tasks.append(
                asyncio.create_task(self._auto_mode_worker(), name="telegram_auto_mode_worker")
            )

            await self.send_notification("System started successfully", priority="INFO")

            await self._stop_event.wait()

            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            queue_task.cancel()
            for t in self._background_tasks:
                t.cancel()
        except Exception as exc:
            self.logger.exception("Failed to start Telegram controller", extra={"component": "telegram"})
            raise exc

    async def stop(self) -> None:
        self._stop_event.set()

    def _register_handlers(self, app: Application) -> None:
        # Command handlers
        app.add_handler(CommandHandler("help", lambda u, c: self._authorized(handlers.cmd_help, u, c)))
        app.add_handler(CommandHandler("status", lambda u, c: self._authorized(handlers.cmd_status, u, c)))
        app.add_handler(CommandHandler("stats", lambda u, c: self._authorized(handlers.cmd_stats, u, c)))
        app.add_handler(CommandHandler("pause", lambda u, c: self._authorized(handlers.cmd_pause, u, c)))
        app.add_handler(CommandHandler("resume", lambda u, c: self._authorized(handlers.cmd_resume, u, c)))
        app.add_handler(CommandHandler("logs", lambda u, c: self._authorized(handlers.cmd_logs, u, c)))
        # Runtime config update (simple key/value)
        app.add_handler(CommandHandler("config", lambda u, c: self._authorized(handlers.cmd_config, u, c)))
        app.add_handler(CommandHandler("daily_summary", lambda u, c: self._authorized(handlers.cmd_daily_summary, u, c)))
        app.add_handler(CommandHandler("pending", lambda u, c: self._authorized(handlers.cmd_pending, u, c)))
        app.add_handler(CommandHandler("report", lambda u, c: self._authorized(handlers.cmd_report, u, c)))
        app.add_handler(CommandHandler("secret", lambda u, c: self._authorized(handlers.cmd_secret, u, c)))
        app.add_handler(CommandHandler("netid", lambda u, c: self._authorized(handlers.cmd_netid, u, c)))
        app.add_handler(CommandHandler("referral", lambda u, c: self._authorized(handlers.cmd_referral, u, c)))
        # Help: show commands
        
        # Action handlers
        app.add_handler(CommandHandler("approve", lambda u, c: self._authorized(handlers.cmd_approve, u, c)))
        app.add_handler(CommandHandler("reject", lambda u, c: self._authorized(handlers.cmd_reject, u, c)))
        app.add_handler(CommandHandler("edit", lambda u, c: self._authorized(handlers.cmd_edit, u, c)))
        app.add_handler(CommandHandler("quickreply", lambda u, c: self._authorized(handlers.cmd_quickreply, u, c)))
        
        # Account management handlers
        app.add_handler(CommandHandler("accounts", lambda u, c: self._authorized(handlers.cmd_accounts, u, c)))
        app.add_handler(CommandHandler("account", lambda u, c: self._authorized(handlers.cmd_account, u, c)))
        app.add_handler(CommandHandler("add_account", lambda u, c: self._authorized(handlers.cmd_add_account, u, c)))
        
        # Callback query and message handlers
        app.add_handler(CallbackQueryHandler(self._on_callback_query))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text_message))

    async def _authorized(self, handler_func, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            chat_id = str(update.effective_chat.id) if update.effective_chat else ""
            user_id = str(update.effective_user.id) if update.effective_user else ""
            if chat_id != self.user_chat_id and user_id != self.user_chat_id:
                if update.effective_chat:
                    await self._send_text(update.effective_chat.id, "Unauthorized.")
                self.logger.warning(
                    "Unauthorized Telegram access",
                    extra={"component": "telegram", "chat_id": chat_id, "user_id": user_id},
                )
                return
            # Mark user as active
            self._last_user_activity = datetime.utcnow()
            if self.auto_mode:
                self.auto_mode = False
                await self.send_notification("👋 Detected activity. Auto-mode dimatikan.", priority="INFO")
                await self._send_blocked_summary()
            await handler_func(self, update, context)
        except Exception as exc:
            await self._safe_notify_error("authorized_handler", exc)

    def _priority_value_from_label(self, label: str) -> int:
        label_upper = (label or "INFO").upper()
        if label_upper in {"CRITICAL", "ALERT"}:
            return 0
        if label_upper in {"ERROR", "WARN", "WARNING"}:
            return 1
        return 2

    async def _escalation_worker(self, interval_seconds: int = 300, stale_seconds: int = 900) -> None:
        """Periodically check for stale pending actions and backlog to escalate."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(interval_seconds)
                now = datetime.utcnow()
                stale_before = now - timedelta(seconds=stale_seconds)
                with self.db.session_scope(logger=self.logger) as session:
                    rows = (
                        session.query(TelegramInteraction)
                        .filter(TelegramInteraction.responded_at.is_(None))
                        .filter(TelegramInteraction.requested_at <= stale_before)
                        .order_by(TelegramInteraction.requested_at.asc())
                        .limit(10)
                        .all()
                    )
                if rows:
                    lines = ["⏰ *Pending actions waiting too long:*"]
                    for row in rows:
                        ctx = row.context or {}
                        action_id = ctx.get("action_id", "n/a")
                        lines.append(f"- {sanitize_markdown(row.action_type)} `{action_id}` since {row.requested_at}")
                    await self.send_notification("\n".join(lines), priority="WARN")

                backlog = self._notification_queue.qsize()
                if backlog > 20:
                    await self.send_notification(
                        f"⚠️ Telegram queue backlog high: {backlog} items", priority="WARN", priority_value=0
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.error(
                    "Escalation worker error",
                    extra={"component": "telegram", "error": str(exc)},
                )
                continue

    async def request_human_input(self, action_type: str, context: Dict[str, Any], timeout: int = 3600) -> HumanInputResult:
        """Request human input and wait for response (blocking)."""
        action_id = uuid.uuid4().hex
        requested_at = datetime.utcnow()

        persisted_context = dict(context or {})
        persisted_context["action_id"] = action_id

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_futures[action_id] = future

        # Persist pending action.
        try:
            with self.db.session_scope(logger=self.logger) as session:
                session.add(
                    TelegramInteraction(
                        action_type=action_type,
                        context=persisted_context,
                        requested_at=requested_at,
                        responded_at=None,
                        response_value=None,
                        timeout=False,
                    )
                )
        except Exception as exc:
            self.logger.error(
                "Failed to persist telegram interaction",
                extra={"component": "telegram", "action_id": action_id, "error": str(exc)},
            )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Approve", callback_data=f"approve:{action_id}"),
                    InlineKeyboardButton("Reject", callback_data=f"reject:{action_id}"),
                ],
                [InlineKeyboardButton("Edit", callback_data=f"edit:{action_id}")],
            ]
        )

        message = self._format_action_message(action_id, action_type, context, timeout)
        await self.send_notification(message, priority="INFO", attachments=None, reply_markup=keyboard)

        timed_out = False
        response_value: Optional[str] = None
        responded_at: Optional[datetime] = None
        result: Optional[Dict[str, Any]] = None

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            response_value = result.get("response_value") if isinstance(result, dict) else None
            responded_at = datetime.utcnow()
        except asyncio.TimeoutError:
            timed_out = True
            responded_at = None
        finally:
            self._pending_futures.pop(action_id, None)

        # Update persistence.
        try:
            with self.db.session_scope(logger=self.logger) as session:
                row = (
                    session.query(TelegramInteraction)
                    .filter(TelegramInteraction.action_type == action_type)
                    .filter(TelegramInteraction.requested_at == requested_at)
                    .first()
                )
                if row is None:
                    # Best-effort fallback: match by action_id stored in JSON context.
                    row = (
                        session.query(TelegramInteraction)
                        .filter(TelegramInteraction.action_type == action_type)
                        .order_by(TelegramInteraction.requested_at.desc())
                        .first()
                    )
                if row is not None:
                    row.responded_at = responded_at
                    row.response_value = response_value
                    row.timeout = timed_out
        except Exception as exc:
            self.logger.error(
                "Failed to update telegram interaction",
                extra={"component": "telegram", "action_id": action_id, "error": str(exc)},
            )

        response_time = None
        if responded_at is not None:
            response_time = (responded_at - requested_at).total_seconds()

        return HumanInputResult(
            action_id=action_id,
            action_type=action_type,
            response_value=response_value,
            responded_at=responded_at,
            response_time_seconds=response_time,
            timeout=timed_out,
        )

    async def request_custom_input(
        self,
        action_type: str,
        context: Dict[str, Any],
        message: str,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        timeout: int = 3600,
        action_id: Optional[str] = None,
    ) -> HumanInputResult:
        """Generic variant of request_human_input allowing custom message and keyboard."""
        action_id = action_id or uuid.uuid4().hex
        requested_at = datetime.utcnow()

        persisted_context = dict(context or {})
        persisted_context["action_id"] = action_id

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_futures[action_id] = future

        try:
            with self.db.session_scope(logger=self.logger) as session:
                session.add(
                    TelegramInteraction(
                        action_type=action_type,
                        context=persisted_context,
                        requested_at=requested_at,
                        responded_at=None,
                        response_value=None,
                        timeout=False,
                    )
                )
        except Exception as exc:
            self.logger.error(
                "Failed to persist telegram interaction",
                extra={"component": "telegram", "action_id": action_id, "error": str(exc)},
            )

        await self.send_notification(
            message,
            priority="INFO",
            reply_markup=reply_markup,
        )

        timed_out = False
        response_value: Optional[str] = None
        responded_at: Optional[datetime] = None
        try:
            result: Dict[str, Any] = await asyncio.wait_for(future, timeout=timeout)
            response_value = result.get("response_value") if isinstance(result, dict) else None
            responded_at = datetime.utcnow()
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            self._pending_futures.pop(action_id, None)

        try:
            with self.db.session_scope(logger=self.logger) as session:
                row = (
                    session.query(TelegramInteraction)
                    .filter(TelegramInteraction.action_type == action_type)
                    .filter(TelegramInteraction.requested_at == requested_at)
                    .first()
                )
                if row is None:
                    row = (
                        session.query(TelegramInteraction)
                        .filter(TelegramInteraction.action_type == action_type)
                        .order_by(TelegramInteraction.requested_at.desc())
                        .first()
                    )
                if row is not None:
                    row.responded_at = responded_at
                    row.response_value = response_value
                    row.timeout = timed_out
        except Exception as exc:
            self.logger.error(
                "Failed to update telegram interaction",
                extra={"component": "telegram", "action_id": action_id, "error": str(exc)},
            )

        response_time = None
        if responded_at is not None:
            response_time = (responded_at - requested_at).total_seconds()

        return HumanInputResult(
            action_id=action_id,
            action_type=action_type,
            response_value=response_value,
            responded_at=responded_at,
            response_time_seconds=response_time,
            timeout=timed_out,
        )

    async def resolve_action(self, action_id: str, response_type: str, response_value: Optional[str], source: str) -> Dict[str, Any]:
        """Resolve a pending action (from command/callback)."""
        fut = self._pending_futures.get(action_id)
        if fut is not None and not fut.done():
            fut.set_result({"response_type": response_type, "response_value": response_value, "source": source})
            return {"success": True}
        return {"success": False, "error": "action_not_found_or_already_completed"}

    async def send_notification(
        self,
        message: str,
        priority: str = "INFO",
        attachments: Optional[list[str]] = None,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        priority_value: Optional[int] = None,
    ) -> None:
        """Queue a notification for sending. Non-blocking."""
        try:
            prio_val = priority_value if priority_value is not None else self._priority_value_from_label(priority)
            await self._notification_queue.put(
                _NotificationItem(
                    priority=prio_val,
                    seq=next(self._notif_seq),
                    message=message,
                    priority_label=priority,
                    attachments=attachments,
                    reply_markup=reply_markup,
                )
            )
        except Exception as exc:
            self.logger.error(
                "Failed to enqueue telegram notification",
                extra={"component": "telegram", "error": str(exc)},
            )

    async def send_file(
        self,
        chat_id: Optional[str],
        file_path: str,
        caption: Optional[str] = None,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> None:
        """Send a file immediately with retry and rate limiting."""
        target_chat = chat_id or self.user_chat_id
        await self._send_file(
            chat_id=str(target_chat),
            file_path=file_path,
            caption=caption,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup,
        )

    async def _queue_worker(self) -> None:
        while True:
            item = await self._notification_queue.get()
            try:
                await self._send_with_rate_limit(item)
            except Exception as exc:
                self.logger.error(
                    "Telegram send failed",
                    extra={"component": "telegram", "error": str(exc)},
                )
                item.attempts += 1
                if item.attempts < 3:
                    # Exponential backoff
                    await asyncio.sleep(min(5 * item.attempts, 30))
                    await self._notification_queue.put(item)
                else:
                    self.logger.error(
                        "Dropping notification after retries",
                        extra={"component": "telegram", "message": item.message[:200]},
                    )
            finally:
                self._notification_queue.task_done()

    async def _send_with_rate_limit(self, item: _NotificationItem) -> None:
        if self._app is None:
            return

        # Rate limit to avoid Telegram spam.
        while not self._send_rate_limiter.try_acquire():
            await asyncio.sleep(0.2)

        msg = item.message
        reply_markup = item.reply_markup

        # split long messages
        chunks = self._split_message(msg, limit=3500)
        for idx, chunk in enumerate(chunks):
            await self._send_text(
                int(self.user_chat_id),
                chunk,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=reply_markup if idx == 0 else None,
            )

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        parse_mode: Optional[str] = ParseMode.MARKDOWN_V2,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> None:
        if self._app is None:
            return
        try:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            self.logger.error(
                "Telegram API error",
                extra={"component": "telegram", "error": str(exc)},
            )
            raise

    async def _send_file(
        self,
        chat_id: int,
        file_path: str,
        caption: Optional[str] = None,
        parse_mode: Optional[str] = None,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        max_retries: int = 3,
    ) -> None:
        if self._app is None:
            return
        if not os.path.exists(file_path):
            self.logger.error(
                "Failed to read file for Telegram send",
                extra={"component": "telegram", "error": "file not found", "path": file_path},
            )
            return

        # Rate limit to avoid Telegram spam.
        while not self._send_rate_limiter.try_acquire():
            await asyncio.sleep(0.2)

        for attempt in range(max_retries):
            try:
                with open(file_path, "rb") as f:
                    await self._app.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        caption=caption,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup,
                    )
                return
            except Exception as exc:
                if attempt == max_retries - 1:
                    self.logger.error(
                        "Telegram send_document error",
                        extra={"component": "telegram", "error": str(exc), "path": file_path},
                    )
                    return
                await asyncio.sleep(2 ** attempt)

    async def _on_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle callback queries from inline keyboards."""
        query = update.callback_query
        await query.answer()
        
        try:
            # System commands
            if query.data == "status":
                await handlers.cmd_status(self, update, context)
            elif query.data == "pause":
                await handlers.cmd_pause(self, update, context)
            elif query.data == "resume":
                await handlers.cmd_resume(self, update, context)
                
            # Logs and monitoring
            elif query.data.startswith("logs"):
                # Handle log level filters
                level = query.data.split(" ")[1] if " " in query.data else None
                context.args = [level] if level else []
                await handlers.cmd_logs(self, update, context)
            elif query.data == "download_logs":
                if self.log_file_path and os.path.exists(self.log_file_path):
                    await self._send_file(
                        update.effective_chat.id,
                        self.log_file_path,
                        caption="📄 *Log File*"
                    )
                else:
                    await self._send_text(
                        update.effective_chat.id,
                        "❌ Log file not found or not accessible.",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
            
            # Account management
            elif query.data == "accounts" or query.data.startswith("accounts:"):
                page = query.data.split(":")[1] if ":" in query.data else "1"
                context.args = [page]
                await handlers.cmd_accounts(self, update, context)
                
            elif query.data.startswith("account:"):
                account_id = query.data.split(":")[1]
                context.args = [account_id]
                await handlers.cmd_account(self, update, context)
                
            elif query.data.startswith("toggle_account:"):
                account_id = query.data.split(":")[1]
                await self._toggle_account_status(update, account_id)
                
            elif query.data.startswith("delete_account:"):
                account_id = query.data.split(":")[1]
                await self._delete_account(update, account_id)
                
            elif query.data.startswith("action:auto:"):
                # Auto-mode helper buttons
                parts = query.data.split(":")
                action = parts[2] if len(parts) >= 3 else ""
                post_id = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else None
                if action == "rotate":
                    await self._handle_auto_rotate(update, post_id)
                elif action == "retry":
                    await self._handle_auto_retry(update, post_id)
                elif action == "skip":
                    await self._handle_auto_skip(update, post_id)

            # Handle action responses (approve/reject/edit)
            elif query.data.startswith(("approve:", "reject:", "edit:")):
                action_type, action_id = query.data.split(":", 1)
                if action_type == "approve":
                    await handlers.cmd_approve(self, update, context, action_id)
                elif action_type == "reject":
                    await handlers.cmd_reject(self, update, context, action_id)
                elif action_type == "edit":
                    # For edit, we need to ask for new content
                    await self._ask_for_edit_content(update, action_id)
                
            # Handle confirmation for account deletion
            elif query.data.startswith("confirm_delete:"):
                account_id = query.data.split(":")[1]
                await self._confirm_delete_account(update, account_id)
            
            # Generic action resolution for human-in-the-loop prompts
            elif query.data.startswith("action:"):
                parts = query.data.split(":")
                if len(parts) >= 3:
                    _, action_id, response_value = parts[0], parts[1], ":".join(parts[2:])
                    await self.resolve_action(
                        action_id=action_id,
                        response_type="callback",
                        response_value=response_value,
                        source="callback",
                    )
                else:
                    await self._safe_notify_error("callback_action_parse", ValueError("Invalid action callback format"))

            # Referral inline callbacks
            elif query.data.startswith("referral_page:"):
                page = query.data.split(":")[1] if ":" in query.data else "1"
                context.args = [page]
                await handlers._send_referral_list(self, update, page=int(page))
            elif query.data.startswith("referral_toggle:"):
                parts = query.data.split(":")
                if len(parts) >= 3:
                    rid = parts[1]
                    on = parts[2]
                    page = int(parts[3]) if len(parts) >= 4 and parts[3].isdigit() else 1
                    context.args = [rid, "on" if on in {"on", "true"} else "off"]
                    await handlers.cmd_referral_toggle(self, update, context, page=page)
            elif query.data == "referral_help":
                await self._send_text(
                    update.effective_chat.id,
                    "Usage:\n/referral list [platform]\n/referral add <platform> <url> [category] [commission]\n/referral toggle <id> <on|off>",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
            elif query.data == "referral_add":
                await handlers.cmd_referral_add_prompt(self, update, context)

        except Exception as exc:
            await self._safe_notify_error("callback_query", exc)
            
    async def _toggle_account_status(self, update: Update, account_id: str) -> None:
        """Toggle the active status of an account."""
        try:
            with self.db.session_scope() as session:
                account = session.query(Account).filter(Account.id == int(account_id)).first()
                if account:
                    account.is_active = not account.is_active
                    status = "activated" if account.is_active else "deactivated"
                    await self._send_text(
                        update.effective_chat.id,
                        f"✅ Account `{account_id}` has been {status}.",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                    # Refresh the account view
                    await self._show_account(update, account_id)
                else:
                    await self._send_text(
                        update.effective_chat.id,
                        f"❌ Account `{account_id}` not found.",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
        except Exception as exc:
            await self._safe_notify_error("toggle_account_status", exc)
            
    async def _delete_account(self, update: Update, account_id: str) -> None:
        """Delete an account after confirmation."""
        try:
            keyboard = [
                [
                    InlineKeyboardButton("❌ Cancel", callback_data=f"account:{account_id}"),
                    InlineKeyboardButton("🗑️ Confirm Delete", callback_data=f"confirm_delete:{account_id}")
                ]
            ]
            
            await self._send_text(
                update.effective_chat.id,
                f"⚠️ *Confirm Deletion*\nAre you sure you want to delete account `{account_id}`? This action cannot be undone.",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except Exception as exc:
            await self._safe_notify_error("delete_account", exc)
            
    async def _confirm_delete_account(self, update: Update, account_id: str) -> None:
        """Handle account deletion after confirmation."""
        try:
            with self.db.session_scope() as session:
                account = session.query(Account).filter(Account.id == int(account_id)).first()
                if account:
                    session.delete(account)
                    await self._send_text(
                        update.effective_chat.id,
                        f"✅ Account `{account_id}` has been deleted.",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
                    # Go back to accounts list
                    await handlers.cmd_accounts(self, update, None)
                else:
                    await self._send_text(
                        update.effective_chat.id,
                        f"❌ Account `{account_id}` not found.",
                        parse_mode=ParseMode.MARKDOWN_V2
                    )
        except Exception as exc:
            await self._safe_notify_error("confirm_delete_account", exc)
            
    async def _show_account(self, update: Update, account_id: str) -> None:
        """Helper to show account details."""
        context = ContextTypes.DEFAULT_TYPE()
        context.args = [account_id]
        await handlers.cmd_account(self, update, context)
        
    async def _show_accounts(self, update: Update, page: int = 1) -> None:
        """Helper to show accounts list."""
        context = ContextTypes.DEFAULT_TYPE()
        context.args = [str(page)]
        await handlers.cmd_accounts(self, update, context)

    async def _on_text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            # Reserved for future conversational flows.
            chat_id = str(update.effective_chat.id) if update.effective_chat else ""
            if chat_id != self.user_chat_id:
                return
            self._last_user_activity = datetime.utcnow()
            if self.auto_mode:
                self.auto_mode = False
                await self.send_notification("👋 Detected activity. Auto-mode dimatikan.", priority="INFO")
                await self._send_blocked_summary()
            # If there are pending actions, treat incoming text as a response to the most recent one.
            if self._pending_futures:
                # Pick the most recently created future (last inserted key)
                action_id = next(reversed(self._pending_futures.keys()))
                await self.resolve_action(
                    action_id=action_id,
                    response_type="text",
                    response_value=update.message.text if update.message else None,
                    source="text_message",
                )
        except Exception:
            return

    async def _auto_mode_worker(self, interval_seconds: int = 300) -> None:
        """Enable auto-mode after prolonged user inactivity."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(interval_seconds)
                now = datetime.utcnow()
                idle_seconds = (now - self._last_user_activity).total_seconds()
                if not self.auto_mode and idle_seconds >= self.idle_threshold_seconds:
                    self.auto_mode = True
                    await self.send_notification(
                        f"🤖 Auto-mode aktif (idle ≥ {self.idle_threshold_seconds//3600} jam). "
                        "Bot akan menjalankan langkah aman (retry/backoff/rotate) secara otomatis bila memungkinkan.",
                        priority="WARN",
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.logger.error(
                    "Auto-mode worker error",
                    extra={"component": "telegram", "error": str(exc)},
                )
                continue

    async def _send_blocked_summary(self, limit: int = 10) -> None:
        """Send summary of blocked_auto, failed, or skipped posts when user returns."""
        try:
            with self.db.session_scope(logger=self.logger) as session:
                blocked = (
                    session.query(Post)
                    .filter(
                        (Post.status == PostStatus.APPROVED)
                        & (Post.metadata_json["blocked_auto"].as_boolean() == True)  # type: ignore[index]
                    )
                    .order_by(Post.updated_at.desc())
                    .limit(limit)
                    .all()
                )
                failed = (
                    session.query(Post)
                    .filter(Post.status == PostStatus.FAILED)
                    .order_by(Post.updated_at.desc())
                    .limit(limit)
                    .all()
                )
                skipped = (
                    session.query(Post)
                    .filter(Post.metadata_json["skip_auto"].as_boolean() == True)  # type: ignore[index]
                    .order_by(Post.updated_at.desc())
                    .limit(limit)
                    .all()
                )
            if not blocked and not failed and not skipped:
                return
            lines = ["⚠️ Ringkasan hambatan saat auto-mode:"]
            if blocked:
                lines.append("• blocked_auto:")
                for row in blocked:
                    reason = (row.metadata_json or {}).get("blocked_reason") or row.error_message or "unknown"
                    lines.append(f"  - post_id={row.id} {row.platform} reason={reason}")
            if failed:
                lines.append("• failed:")
                for row in failed:
                    lines.append(f"  - post_id={row.id} {row.platform} err={row.error_message or 'unknown'}")
            if skipped:
                lines.append("• skipped (skip_auto):")
                for row in skipped:
                    lines.append(f"  - post_id={row.id} {row.platform}")
            await self.send_notification("\n".join(lines), priority="WARN")
        except Exception as exc:
            self.logger.error(
                "Failed to send blocked summary",
                extra={"component": "telegram", "error": str(exc)},
            )

    def consume_auto_retry(self, max_items: int = 10) -> list[int]:
        """Drain queued auto-retry requests."""
        items: list[int] = []
        for _ in range(max_items):
            try:
                items.append(self._auto_retry_queue.get_nowait())
            except QueueEmpty:
                break
        return items

    def consume_auto_rotate(self, max_items: int = 10) -> list[int]:
        """Drain queued auto-rotate requests."""
        items: list[int] = []
        for _ in range(max_items):
            try:
                items.append(self._auto_rotate_queue.get_nowait())
            except QueueEmpty:
                break
        return items

    async def _handle_auto_rotate(self, update: Update, post_id: Optional[int]) -> None:
        """Queue rotate+retry request."""
        try:
            if post_id is not None:
                self._auto_rotate_queue.put_nowait(post_id)
            await self._send_text(
                update.effective_chat.id,
                "🔄 Rotate diminta. Sistem akan memakai akun/proxy/UA berikutnya pada percobaan berikut.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as exc:
            await self._safe_notify_error("auto_rotate", exc)

    async def _handle_auto_retry(self, update: Update, post_id: Optional[int]) -> None:
        """Queue retry request."""
        try:
            if post_id is not None:
                self._auto_retry_queue.put_nowait(post_id)
            await self._send_text(
                update.effective_chat.id,
                "🔁 Retry diminta. Bot akan menjadwalkan ulang posting untuk post tersebut.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as exc:
            await self._safe_notify_error("auto_retry", exc)

    async def _handle_auto_skip(self, update: Update, post_id: Optional[int]) -> None:
        """Mark skip request (set metadata flag)."""
        try:
            if post_id is not None:
                with self.db.session_scope(logger=self.logger) as session:
                    post = session.query(Post).filter(Post.id == post_id).first()
                    if post:
                        meta = post.metadata_json or {}
                        meta["skip_auto"] = True
                        post.metadata_json = meta
                        session.add(post)
            await self._send_text(
                update.effective_chat.id,
                "⏭️ Ditandai skip. Tidak ada tindakan lanjutan otomatis.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception as exc:
            await self._safe_notify_error("auto_skip", exc)

    async def _rehydrate_pending_actions(self) -> None:
        """Rehydrate pending actions so bot can survive restart.

        We only notify about their presence; actual resolution requires new action_id.
        """
        try:
            with self.db.session_scope(logger=self.logger) as session:
                pending = (
                    session.query(TelegramInteraction)
                    .filter(TelegramInteraction.responded_at.is_(None))
                    .filter(TelegramInteraction.timeout.is_(False))
                    .order_by(TelegramInteraction.requested_at.asc())
                    .limit(50)
                    .all()
                )
            if pending:
                await self.send_notification(
                    f"Recovered {len(pending)} pending Telegram interactions from previous run.",
                    priority="WARNING",
                )
        except Exception as exc:
            self.logger.error(
                "Failed to rehydrate pending actions",
                extra={"component": "telegram", "error": str(exc)},
            )

    def _format_action_message(self, action_id: str, action_type: str, context: Dict[str, Any], timeout: int) -> str:
        safe_context = sanitize_markdown(str(context))
        return (
            f"*Action Required*\n"
            f"*action_id*: {sanitize_markdown(action_id)}\n"
            f"*type*: {sanitize_markdown(action_type)}\n"
            f"*timeout*: {sanitize_markdown(str(timeout))} seconds\n\n"
            f"*context*:\n{safe_context}"
        )

    def _split_message(self, text: str, limit: int = 3500) -> list[str]:
        if not text:
            return [""]
        # We assume text already markdown-escaped.
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        current = ""
        for line in text.splitlines(True):
            if len(current) + len(line) > limit:
                chunks.append(current)
                current = ""
            current += line
        if current:
            chunks.append(current)
        return chunks

    async def _safe_notify_error(self, context: str, error: Exception, error_code: str | None = None) -> None:
        """Safely send an error notification with guided remediation playbook."""
        code = (error_code or getattr(error, "code", None) or context or "unknown").strip().lower()
        playbook = build_playbook(code)
        self.logger.error("Error in %s [%s]: %s", context, code, str(error), exc_info=error)
        steps = "\n".join([f"{idx+1}. {step}" for idx, step in enumerate(playbook.steps)])
        text = f"{playbook.title}\nContext: `{context}`\nError: `{sanitize_markdown(str(error))}`\n\nLangkah:\n{steps}"

        buttons = []
        retry_cb = f"action:{uuid.uuid4()}:retry"
        if playbook.allow_retry:
            buttons.append(InlineKeyboardButton("🔁 Retry (setelah langkah)", callback_data=retry_cb))
        if playbook.allow_rotate:
            buttons.append(InlineKeyboardButton("🔄 Rotate akun/proxy/UA", callback_data="action:auto:rotate"))
        if playbook.allow_skip:
            buttons.append(InlineKeyboardButton("⏭️ Skip", callback_data="action:auto:skip"))
        reply_markup = InlineKeyboardMarkup([buttons]) if buttons else None

        try:
            await self.send_notification(text, priority="ERROR", reply_markup=reply_markup)
        except Exception:
            self.logger.exception("Failed to send error notification", extra={"component": "telegram"})
            
    async def _send_text(
        self, 
        chat_id: str, 
        text: str, 
        parse_mode: Optional[str] = None,
        reply_markup = None,
        disable_web_page_preview: bool = True,
        max_retries: int = 3
    ) -> bool:
        """Send a text message with retry logic and rate limiting."""
        if not self._app or not self._app.bot:
            self.logger.warning("Cannot send message: Bot not initialized")
            return False
            
        # Apply rate limiting
        await self._send_rate_limiter.acquire()
        
        for attempt in range(max_retries):
            try:
                await self._app.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                    disable_web_page_preview=disable_web_page_preview
                )
                return True
                
            except Exception as e:
                if attempt == max_retries - 1:  # Last attempt
                    self.logger.error(
                        f"Failed to send message after {max_retries} attempts",
                        exc_info=e
                    )
                    return False
                    
                # Exponential backoff
                await asyncio.sleep(2 ** attempt)
                
        return False
        
    async def _send_file(
        self, 
        chat_id: str, 
        file_path: str, 
        caption: Optional[str] = None,
        parse_mode: Optional[str] = None,
        reply_markup = None,
        max_retries: int = 3
    ) -> bool:
        """Send a file with retry logic and rate limiting."""
        if not self._app or not self._app.bot:
            self.logger.warning("Cannot send file: Bot not initialized")
            return False
            
        if not os.path.exists(file_path):
            self.logger.error(f"File not found: {file_path}")
            return False
            
        # Apply rate limiting
        await self._send_rate_limiter.acquire()
        
        for attempt in range(max_retries):
            try:
                with open(file_path, 'rb') as file:
                    await self._app.bot.send_document(
                        chat_id=chat_id,
                        document=file,
                        filename=os.path.basename(file_path),
                        caption=caption,
                        parse_mode=parse_mode,
                        reply_markup=reply_markup
                    )
                return True
                
            except Exception as e:
                if attempt == max_retries - 1:  # Last attempt
                    self.logger.error(
                        f"Failed to send file after {max_retries} attempts: {file_path}",
                        exc_info=e
                    )
                    return False
                    
                # Exponential backoff
                await asyncio.sleep(2 ** attempt)
                
        return False
