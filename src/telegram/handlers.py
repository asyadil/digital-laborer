"""Command handler implementations for the Telegram bot.

This module contains all the command handlers for the Telegram bot, including:
- System commands (help, status, pause, resume, logs)
- Action handlers (approve, reject, edit)
- Account management (list, view, add, edit, delete accounts)
- Quick reply functionality
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from sqlalchemy import func
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
from telegram.ext import ContextTypes

from src.database.models import (
    Account,
    AccountType,
    TelegramInteraction,
    Post,
    PostStatus,
    SystemMetric,
    ReferralLink,
)
from src.database.operations import DatabaseSessionManager
from src.utils.rate_limiter import FixedWindowRateLimiter
from src.utils.validators import sanitize_markdown, validate_email, validate_url
from src.monitoring.analytics import Analytics
from src.monitoring.audit import AuditLogger


def _format_kv(title: str, value: Any, max_length: int = 200) -> str:
    """Format key-value pair for MarkdownV2 output.
    
    Args:
        title: The key/title to display
        value: The value to display
        max_length: Maximum length of the value before truncation
        
    Returns:
        Formatted string with markdown formatting
    """
    # Truncate long values
    str_value = str(value)
    if len(str_value) > max_length:
        str_value = str_value[:max_length] + "..."
        
    return f"*{sanitize_markdown(str(title))}*: {sanitize_markdown(str_value)}"


async def cmd_help(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help message with available commands and usage.
    
    Args:
        controller: The TelegramController instance
        update: The Telegram update object
        context: The callback context
    """
    try:
        help_text = """
🤖 *Bot Commands Help*

📊 *System*  
/status - Show system status and metrics  
/logs [level] [lines] - View logs (default: 200 lines)  

⚙️ *Control*  
/pause - Pause all automation  
/resume - Resume automation  
/restart - Restart the bot (admin only)  

🔄 *Action Management*  
/approve <action_id> - Approve pending action  
/reject <action_id> - Reject pending action  
/edit <action_id> <text> - Edit and approve action  
/quickreply <action_id> <text> - Quick reply to action  

👥 *Account Management*  
/accounts [page] - List all accounts (paginated)  
/account <id> - Show account details  
/add_account <platform> <username> [email] [--key=value] - Add new account  

🔍 *Examples*:  
`/add_account quora my_username my@email.com`  
`/add_account reddit myreddituser --is_active=true`  

📝 *Note*: Replace <value> with actual values, no brackets needed. Use backticks (`) for code formatting.
"""
        await controller._send_text(
            chat_id=update.effective_chat.id,
            text=help_text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True
        )
    except Exception as exc:
        controller.logger.error(
            "Error in help command",
            extra={"component": "telegram_handlers", "error": str(exc)},
            exc_info=True
        )
        await controller._safe_notify_error("cmd_help", exc)


async def cmd_status(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show system status and statistics."""
    try:
        uptime = datetime.utcnow() - controller.started_at
        
        # Get database stats
        db_stats = {}
        try:
            with controller.db.session_scope() as session:
                db_stats = {
                    'total_accounts': session.query(Account).count(),
                    'active_accounts': session.query(Account).filter(Account.is_active == True).count()
                }
        except Exception as db_exc:
            controller.logger.error(f"Error getting DB stats: {db_exc}")
        
        # Format status message
        status_lines = [
            "🔄 *System Status*",
            _format_kv("Uptime", str(uptime).split(".")[0]),
            _format_kv("Status", "⏸ Paused" if controller.paused else "▶️ Running"),
            _format_kv("Pending Actions", controller.pending_actions_count),
            "",
            "📊 *Database*",
            _format_kv("Total Accounts", db_stats.get('total_accounts', 'N/A')),
            _format_kv("Active Accounts", db_stats.get('active_accounts', 'N/A')),
        ]
        
        # Add quick action buttons
        keyboard = [
            [
                InlineKeyboardButton("⏸ Pause", callback_data="pause"),
                InlineKeyboardButton("▶️ Resume", callback_data="resume")
            ],
            [
                InlineKeyboardButton("🔄 Refresh", callback_data="status"),
                InlineKeyboardButton("📋 Accounts", callback_data="accounts")
            ]
        ]
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await controller._send_text(
            update.effective_chat.id,
            "\n".join(status_lines),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup
        )
        
    except Exception as exc:
        controller.logger.error(f"Error in status command: {exc}", exc_info=True)
        await controller._safe_notify_error("cmd_status", exc)


async def cmd_stats(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show aggregated stats for posts and metrics."""
    try:
        args = context.args or []
        timeframe = (args[0].lower() if args else "day").strip()
        now = datetime.utcnow()
        if timeframe in {"day", "today"}:
            since = now - timedelta(days=1)
            label = "24h"
        elif timeframe in {"week", "7d"}:
            since = now - timedelta(days=7)
            label = "7d"
        elif timeframe in {"month", "30d"}:
            since = now - timedelta(days=30)
            label = "30d"
        else:
            since = None
            label = "all-time"

        with controller.db.session_scope() as session:
            posted_query = session.query(Post.platform, func.count(Post.id)).filter(Post.status == PostStatus.POSTED)
            pending_query = session.query(Post.platform, func.count(Post.id)).filter(Post.status == PostStatus.PENDING)
            if since:
                posted_query = posted_query.filter(Post.created_at >= since)
                pending_query = pending_query.filter(Post.created_at >= since)
            posted = {row[0]: row[1] for row in posted_query.group_by(Post.platform)}
            pending = {row[0]: row[1] for row in pending_query.group_by(Post.platform)}

            # System metrics (optional)
            metrics = {}
            if since:
                metric_rows = (
                    session.query(SystemMetric.metric_type, func.avg(SystemMetric.value))
                    .filter(SystemMetric.timestamp >= since)
                    .group_by(SystemMetric.metric_type)
                    .all()
                )
                metrics = {row[0]: round(float(row[1]), 3) for row in metric_rows}

        lines = [
            f"📊 *Stats* ({label})",
            "• Posted per platform:",
        ]
        if posted:
            for plat, count in posted.items():
                lines.append(f"  - {plat}: {count}")
        else:
            lines.append("  - none")
        lines.append("• Pending per platform:")
        if pending:
            for plat, count in pending.items():
                lines.append(f"  - {plat}: {count}")
        else:
            lines.append("  - none")
        if metrics:
            lines.append("• Metrics (avg):")
            for mtype, val in metrics.items():
                lines.append(f"  - {mtype}: {val}")

        keyboard = [
            [InlineKeyboardButton("24h", callback_data="stats day"), InlineKeyboardButton("7d", callback_data="stats week")],
            [InlineKeyboardButton("30d", callback_data="stats month"), InlineKeyboardButton("All", callback_data="stats all")],
            [InlineKeyboardButton("🔄 Refresh", callback_data="stats")],
        ]

        await controller._send_text(
            update.effective_chat.id,
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception as exc:
        controller.logger.error(f"Error in stats command: {exc}", exc_info=True)
        await controller._safe_notify_error("cmd_stats", exc)


async def cmd_pause(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        controller.paused = True
        await controller._send_text(update.effective_chat.id, "Paused automation.", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as exc:
        await controller._safe_notify_error("cmd_pause", exc)


async def cmd_resume(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        controller.paused = False
        await controller._send_text(update.effective_chat.id, "Resumed automation.", parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as exc:
        await controller._safe_notify_error("cmd_resume", exc)


async def cmd_config(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View or update a limited set of runtime configs (in-memory, non-persistent)."""
    try:
        args = context.args or []
        if not args:
            await controller._send_text(
                update.effective_chat.id,
                "Usage: /config <key> [value]\nAllowed keys: logging.level, monitoring.health_check_interval, retry.base_delay, retry.max_delay",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        key = args[0]
        value = args[1] if len(args) > 1 else None

        # Whitelist of allowed keys mapped to (object, attr)
        allowed = {
            "logging.level": ("logging", "level"),
            "monitoring.health_check_interval": ("monitoring", "health_check_interval"),
            "retry.base_delay": ("retry", "base_delay"),
            "retry.max_delay": ("retry", "max_delay"),
            "auto.min_quality_auto_post": ("auto", "min_quality_auto_post"),
            "auto.batch_size": ("auto", "batch_size"),
            "auto.enabled": ("auto", "enabled"),
        }
        if key not in allowed:
            await controller._send_text(
                update.effective_chat.id,
                f"❌ Unsupported key `{key}`.\nAllowed: {', '.join(allowed.keys())}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        section_name, attr_name = allowed[key]
        section = getattr(controller.config, section_name, None)
        if section is None:
            # create simple namespace for auto section
            class _NS:
                pass

            if section_name == "auto":
                section = _NS()
                setattr(controller.config, section_name, section)
            else:
                await controller._send_text(
                    update.effective_chat.id,
                    f"❌ Config section `{section_name}` not found.",
                    parse_mode=ParseMode.MARKDOWN_V2,
                )
                return

        if value is None:
            current = getattr(section, attr_name, "N/A")
            await controller._send_text(
                update.effective_chat.id,
                f"ℹ️ `{key}` = `{current}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # Cast value based on existing type
        current_val = getattr(section, attr_name, None)
        new_val = value
        try:
            if isinstance(current_val, bool):
                new_val = value.lower() in {"1", "true", "yes", "on"}
            elif isinstance(current_val, int):
                new_val = int(value)
            else:
                new_val = value
        except Exception as exc:
            await controller._send_text(
                update.effective_chat.id,
                f"❌ Invalid value for `{key}`: {value} ({exc})",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        setattr(section, attr_name, new_val)
        await controller._send_text(
            update.effective_chat.id,
            f"✅ Updated `{key}` to `{new_val}` (in-memory).",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:
        controller.logger.error(f"Error in config command: {exc}", exc_info=True)
        await controller._safe_notify_error("cmd_config", exc)


def _env_path() -> Path:
    return Path(os.getenv("APP_BASE_PATH", Path.cwd())) / ".env"


def _load_env_file(path: Path) -> Dict[str, str]:
    vals: Dict[str, str] = {}
    if not path.exists():
        return vals
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            vals[k.strip()] = v.strip()
    return vals


def _save_env_file(path: Path, data: Dict[str, str]) -> None:
    lines = [f"{k}={v}" for k, v in data.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _parse_csv_list(raw: str) -> List[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


async def cmd_secret(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or get secrets via Telegram (writes to .env and process env)."""
    try:
        args = context.args or []
        if not args:
            await controller._send_text(
                update.effective_chat.id,
                "Usage: /secret NAME=VALUE  (set) or /secret NAME (get masked)",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        env_path = _env_path()
        data = _load_env_file(env_path)
        arg = args[0]
        if "=" not in arg:
            key = arg.strip()
            val = os.getenv(key) or data.get(key) or ""
            masked = "***" if val else "(not set)"
            await controller._send_text(
                update.effective_chat.id,
                f"{key} = {masked}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        key, val = arg.split("=", 1)
        key = key.strip()
        val = val.strip()
        data[key] = val
        _save_env_file(env_path, data)
        os.environ[key] = val
        try:
            AuditLogger.log(
                actor=str(update.effective_user.id) if update.effective_user else "unknown",
                action="secret_update",
                target=key,
                metadata={"chat_id": str(update.effective_chat.id) if update.effective_chat else None},
            )
        except Exception:
            pass
        await controller._send_text(
            update.effective_chat.id,
            f"✅ Secret `{key}` updated (stored in .env).",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:
        controller.logger.error("Error in secret command", exc_info=True)
        await controller._safe_notify_error("cmd_secret", exc)


async def cmd_netid(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manage proxies/user-agents per platform via Telegram."""
    try:
        args = context.args or []
        if not args:
            await controller._send_text(
                update.effective_chat.id,
                "Usage:\n"
                "/netid <platform> show\n"
                "/netid <platform> proxies=<p1,p2> [ua=<ua1,ua2>]\n"
                "Platforms: reddit, youtube, quora, tiktok, instagram, facebook",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        platform = args[0].lower()
        allowed_platforms = {"reddit", "youtube", "quora", "tiktok", "instagram", "facebook"}
        if platform not in allowed_platforms:
            await controller._send_text(
                update.effective_chat.id,
                f"❌ Platform `{platform}` tidak dikenal. Pilih: {', '.join(sorted(allowed_platforms))}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        env_path = _env_path()
        data = _load_env_file(env_path)
        cfg = getattr(getattr(controller.config, "platforms", None), platform, None)

        # Show current values
        if len(args) == 1 or args[1].lower() in {"show", "list"}:
            proxies = getattr(cfg, "proxies", []) if cfg else []
            uas = getattr(cfg, "user_agents", []) if cfg else []
            lines = [
                f"🔌 Network identity for *{platform}*",
                f"- proxies ({len(proxies)}): {', '.join(proxies)[:180] or '(empty)'}",
                f"- user_agents ({len(uas)}): {', '.join(uas)[:180] or '(empty)'}",
                "_Catatan_: perubahan runtime akan aktif untuk siklus berikutnya; jika adapter sudah aktif, restart ringan disarankan.",
            ]
            await controller._send_text(update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
            return

        new_proxies: Optional[List[str]] = None
        new_uas: Optional[List[str]] = None
        for token in args[1:]:
            if token.startswith("proxies="):
                new_proxies = _parse_csv_list(token.split("=", 1)[1])
            if token.startswith("ua=") or token.startswith("user_agents="):
                new_uas = _parse_csv_list(token.split("=", 1)[1])
        if new_proxies is None and new_uas is None:
            await controller._send_text(
                update.effective_chat.id,
                "❌ Tidak ada parameter yang diubah. Gunakan proxies=... atau ua=...",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        # Apply in-memory config if available
        if cfg:
            if new_proxies is not None and hasattr(cfg, "proxies"):
                try:
                    cfg.proxies = new_proxies
                except Exception:
                    pass
            if new_uas is not None and hasattr(cfg, "user_agents"):
                try:
                    cfg.user_agents = new_uas
                except Exception:
                    pass

        # Persist to .env for restart persistence
        prefix = platform.upper()
        if new_proxies is not None:
            data[f"{prefix}_PROXIES"] = ",".join(new_proxies)
            os.environ[f"{prefix}_PROXIES"] = ",".join(new_proxies)
        if new_uas is not None:
            data[f"{prefix}_USER_AGENTS"] = ",".join(new_uas)
            os.environ[f"{prefix}_USER_AGENTS"] = ",".join(new_uas)
        _save_env_file(env_path, data)

        lines = [f"✅ Updated network identity for *{platform}*:"]
        if new_proxies is not None:
            lines.append(f"- proxies set ({len(new_proxies)})")
        if new_uas is not None:
            lines.append(f"- user_agents set ({len(new_uas)})")
        lines.append("_Catatan_: adapter yang sudah aktif mungkin perlu restart ringan agar pool baru dipakai._")
        await controller._send_text(update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
        controller.notify_network_change(platform)
        try:
            AuditLogger.log(
                actor=str(update.effective_user.id) if update.effective_user else "unknown",
                action="netid_update",
                target=platform,
                metadata={
                    "chat_id": str(update.effective_chat.id) if update.effective_chat else None,
                    "proxies_count": len(new_proxies or []),
                    "ua_count": len(new_uas or []),
                },
            )
        except Exception:
            pass
    except Exception as exc:
        controller.logger.error("Error in netid command", exc_info=True)
        await controller._safe_notify_error("cmd_netid", exc)


async def cmd_referral(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manage referral links: list/add/toggle."""
    try:
        args = context.args or []
        if not args or args[0] == "list":
            platform = args[1] if len(args) > 1 else None
            with controller.db.session_scope(logger=controller.logger) as session:
                q = session.query(ReferralLink)
                if platform:
                    q = q.filter(ReferralLink.platform_name == platform)
                links = q.order_by(ReferralLink.id.desc()).limit(20).all()
            if not links:
                await controller._send_text(update.effective_chat.id, "No referral links.", parse_mode=ParseMode.MARKDOWN_V2)
                return
            lines = ["🔗 Referral links:"]
            for link in links:
                status = "✅" if link.active else "⏸️"
                lines.append(f"{status} id={link.id} {link.platform_name} {link.url}")
            await controller._send_text(update.effective_chat.id, "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)
            return

        action = args[0]
        if action == "add" and len(args) >= 3:
            platform = args[1]
            url = args[2]
            category = args[3] if len(args) >= 4 else None
            commission = float(args[4]) if len(args) >= 5 else 0.0
            with controller.db.session_scope(logger=controller.logger) as session:
                rl = ReferralLink(
                    platform_name=platform,
                    url=url,
                    category=category,
                    commission_rate=commission,
                    active=True,
                )
                session.add(rl)
            await controller._send_text(
                update.effective_chat.id,
                f"✅ Added referral link id={rl.id} for {platform}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        if action == "toggle" and len(args) >= 3:
            rid = int(args[1])
            on = args[2].lower() in {"1", "true", "on", "yes", "enable", "aktif"}
            with controller.db.session_scope(logger=controller.logger) as session:
                rl = session.query(ReferralLink).filter(ReferralLink.id == rid).first()
                if not rl:
                    await controller._send_text(update.effective_chat.id, f"❌ Referral id {rid} not found", parse_mode=ParseMode.MARKDOWN_V2)
                    return
                rl.active = on
                session.add(rl)
            await controller._send_text(
                update.effective_chat.id,
                f"✅ Referral id={rid} {'enabled' if on else 'disabled'}",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return

        await controller._send_text(
            update.effective_chat.id,
            "Usage:\n/referral list [platform]\n/referral add <platform> <url> [category] [commission]\n/referral toggle <id> <on|off>",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:
        controller.logger.error("Error in referral command", exc_info=True)
        await controller._safe_notify_error("cmd_referral", exc)


async def cmd_daily_summary(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send a quick daily summary (uses existing metrics/posts)."""
    try:
        now = datetime.utcnow()
        since = now - timedelta(days=1)
        with controller.db.session_scope() as session:
            posted = (
                session.query(Post.platform, func.count(Post.id))
                .filter(Post.status == PostStatus.POSTED)
                .filter(Post.created_at >= since)
                .group_by(Post.platform)
                .all()
            )
            pending = (
                session.query(Post.platform, func.count(Post.id))
                .filter(Post.status == PostStatus.PENDING)
                .filter(Post.created_at >= since)
                .group_by(Post.platform)
                .all()
            )
        lines = ["📈 *Daily Summary (24h)*"]
        if posted:
            lines.append("• Posted:")
            for plat, cnt in posted:
                lines.append(f"  - {plat}: {cnt}")
        else:
            lines.append("• Posted: none")
        if pending:
            lines.append("• Pending drafts:")
            for plat, cnt in pending:
                lines.append(f"  - {plat}: {cnt}")
        await controller._send_text(
            update.effective_chat.id,
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:
        controller.logger.error(f"Error in daily_summary command: {exc}", exc_info=True)
        await controller._safe_notify_error("cmd_daily_summary", exc)


async def cmd_pending(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List pending human actions with age."""
    try:
        args = context.args or []
        limit = 20
        if args:
            try:
                limit = max(5, min(int(args[0]), 50))
            except ValueError:
                pass
        with controller.db.session_scope(logger=controller.logger) as session:
            rows = (
                session.query(TelegramInteraction)
                .filter(TelegramInteraction.responded_at.is_(None))
                .order_by(TelegramInteraction.requested_at.asc())
                .limit(limit)
                .all()
            )
        if not rows:
            await controller._send_text(update.effective_chat.id, "✅ No pending actions.", parse_mode=ParseMode.MARKDOWN_V2)
            return
        lines = ["🕒 *Pending Actions*"]
        now = datetime.utcnow()
        for row in rows:
            ctx = row.context or {}
            action_id = ctx.get("action_id", "n/a")
            age_min = max(0, int((now - row.requested_at).total_seconds() // 60))
            lines.append(f"- `{action_id}` {sanitize_markdown(row.action_type)} · {age_min}m ago")
        await controller._send_text(
            update.effective_chat.id,
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as exc:
        controller.logger.error(f"Error in pending command: {exc}", exc_info=True)
        await controller._safe_notify_error("cmd_pending", exc)


async def cmd_logs(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View system logs with filtering options."""
    try:
        args = context.args or []
        level = args[0].upper() if len(args) >= 1 and args[0].upper() in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] else None
        
        try:
            lines = int(args[1]) if len(args) >= 2 else 200
            lines = max(10, min(lines, 2000))
        except (ValueError, IndexError):
            lines = 200

        log_path = controller.log_file_path
        if not log_path or not os.path.exists(log_path):
            await controller._send_text(
                update.effective_chat.id,
                "❌ Log file not found. Please check the log file path in the configuration.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return

        # Read log file with error handling
        try:
            content = _tail_file(log_path, lines=lines)
            
            # Apply level filter if specified
            if level:
                filtered_lines = []
                for line in content.splitlines():
                    if f"- {level} -" in line:
                        filtered_lines.append(line)
                content = "\n".join(filtered_lines)
            
            # Format log content
            if not content.strip():
                content = "No log entries found" + (f" for level {level}" if level else "") + "."
            
            # Create keyboard for log levels
            keyboard = [
                [
                    InlineKeyboardButton(f"{'✅ ' if level == 'DEBUG' else ''}DEBUG", callback_data="logs DEBUG"),
                    InlineKeyboardButton(f"{'✅ ' if level == 'INFO' else ''}INFO", callback_data="logs INFO"),
                ],
                [
                    InlineKeyboardButton(f"{'✅ ' if level == 'WARNING' else ''}WARNING", callback_data="logs WARNING"),
                    InlineKeyboardButton(f"{'✅ ' if level == 'ERROR' else ''}ERROR", callback_data="logs ERROR"),
                ],
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data=f"logs {level or ''}"),
                    InlineKeyboardButton("📥 Download Full Logs", callback_data="download_logs")
                ]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Send as file if content is too large
            if len(content) > 3500:
                await controller._send_file(
                    update.effective_chat.id,
                    log_path,
                    caption=f"📝 *Logs* (Last {lines} lines{f' - Filter: {level}' if level else ''})",
                    reply_markup=reply_markup,
                    parse_mode=ParseMode.MARKDOWN_V2
                )
            else:
                header = f"📝 *Logs* (Last {lines} lines{f' - Filter: {level}' if level else ''})\n\n"
                await controller._send_text(
                    update.effective_chat.id,
                    header + f"```\n{content}\n```",
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=reply_markup
                )
                
        except Exception as file_error:
            error_msg = f"❌ Error reading log file: {str(file_error)}"
            controller.logger.error(error_msg, exc_info=True)
            await controller._send_text(update.effective_chat.id, error_msg)
            
    except Exception as exc:
        controller.logger.error(f"Error in logs command: {exc}", exc_info=True)
        await controller._safe_notify_error("cmd_logs", exc)


def _tail_file(path: str, lines: int = 200) -> str:
    p = Path(path)
    data = p.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-lines:])


async def cmd_approve(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_action_response(controller, update, context, response_type="approve")


async def cmd_reject(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_action_response(controller, update, context, response_type="reject")


async def cmd_edit(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _cmd_action_response(controller, update, context, response_type="edit")


async def _cmd_action_response(controller, update: Update, context: ContextTypes.DEFAULT_TYPE, response_type: str) -> None:
    """Handle action responses (approve/reject/edit)."""
    try:
        args = context.args or []
        if not args:
            await controller._send_text(
                update.effective_chat.id,
                f"❌ Missing action_id. Usage: /{response_type} <action_id>" + 
                (" <new_content>" if response_type == "edit" else ""),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
            
        action_id = args[0]
        new_content = None
        
        # For edit command, require new content
        if response_type == "edit" and len(args) < 2:
            await controller._send_text(
                update.effective_chat.id,
                f"❌ Missing new content. Usage: /edit {action_id} <new_content>",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        elif response_type == "edit":
            new_content = " ".join(args[1:])
        
        # Resolve the action
        result = await controller.resolve_action(
            action_id=action_id,
            response_type=response_type,
            response_value=new_content,
            source="command",
        )
        
        if result.get('success', False):
            # Create keyboard for quick actions
            keyboard = [
                [
                    InlineKeyboardButton("🔄 View Status", callback_data="status"),
                    InlineKeyboardButton("📋 View All Actions", callback_data="list_actions")
                ]
            ]
            
            await controller._send_text(
                update.effective_chat.id,
                f"✅ Successfully {response_type}d action `{action_id}`" + 
                (f" with new content" if new_content else "") + ".",
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            error_msg = result.get('error', 'Unknown error')
            await controller._send_text(
                update.effective_chat.id,
                f"❌ Failed to {response_type} action `{action_id}`: {error_msg}",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            
    except Exception as exc:
        controller.logger.error(f"Error in {response_type} command: {exc}", exc_info=True)
        await controller._safe_notify_error(f"cmd_{response_type}", exc)


async def cmd_accounts(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all registered accounts with pagination."""
    try:
        page = int(context.args[0]) if context.args and context.args[0].isdigit() else 1
        per_page = 10
        offset = (page - 1) * per_page
        
        with controller.db.session_scope() as session:
            # Get total count and paginated accounts
            total = session.query(Account).count()
            accounts = session.query(Account).order_by(Account.created_at.desc()).offset(offset).limit(per_page).all()
            
            if not accounts and page > 1:
                # If page is empty but not the first page, go to last page
                last_page = (total - 1) // per_page + 1
                return await cmd_accounts(controller, update, context, last_page)
            
            # Format accounts list
            account_lines = []
            for acc in accounts:
                status = "✅" if acc.is_active else "⏸️"
                account_lines.append(f"{status} *{acc.platform.value.upper()}*: {acc.username or acc.email or 'N/A'}")
            
            # Create pagination buttons
            total_pages = (total + per_page - 1) // per_page
            keyboard = []
            
            if total_pages > 1:
                row = []
                if page > 1:
                    row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"accounts:{page-1}"))
                if page < total_pages:
                    row.append(InlineKeyboardButton("Next ➡️", callback_data=f"accounts:{page+1}"))
                keyboard.append(row)
            
            keyboard.append([InlineKeyboardButton("➕ Add Account", callback_data="add_account")])
            
            # Prepare message
            message = [
                f"👥 *Accounts* (Page {page}/{total_pages}, Total: {total})",
                "",
                *account_lines,
                "",
                f"Use `/account <id>` to view details or click buttons below."
            ]
            
            await controller._send_text(
                update.effective_chat.id,
                "\n".join(message),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None
            )
            
    except Exception as exc:
        controller.logger.error(f"Error listing accounts: {exc}", exc_info=True)
        await controller._send_text(
            update.effective_chat.id,
            "❌ Failed to list accounts. Please check logs for details.",
            parse_mode=ParseMode.MARKDOWN_V2
        )


async def cmd_account(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show details of a specific account.
    
    Args:
        controller: The TelegramController instance
        update: The Telegram update object
        context: The callback context with account_id as first argument
    """
    try:
        # Validate input
        if not context.args or not context.args[0].isdigit():
            await controller._send_text(
                chat_id=update.effective_chat.id,
                text=(
                    "❌ *Invalid or missing account ID*\n\n"
                    "Usage: `/account <id>`\n"
                    "Example: `/account 1`"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 List Accounts", callback_data="accounts:1")]
                ])
            )
            return
            
        account_id = int(context.args[0])
        
        with controller.db.session_scope() as session:
            # Get account with related data if needed
            account = session.query(Account).filter(Account.id == account_id).first()
            
            if not account:
                await controller._send_text(
                    chat_id=update.effective_chat.id,
                    text=f"❌ Account with ID `{account_id}` not found.",
                    parse_mode=ParseMode.MARKDOWN_V2,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📋 List Accounts", callback_data="accounts:1")]
                    ])
                )
                return
            
            # Format account details with proper escaping
            details = [
                f"👤 *Account Details* (#{account.id})",
                "",
                _format_kv("Platform", account.platform.value.upper()),
                _format_kv("Username", account.username or "N/A"),
                _format_kv("Email", account.email or "N/A"),
                _format_kv("Status", "🟢 Active" if account.is_active else "⏸️ Paused"),
                _format_kv("Created", account.created_at.strftime("%Y-%m-%d %H:%M")),
            ]
            
            # Add metadata if exists
            if account.metadata:
                details.extend(["", "*Metadata*:"])
                for key, value in account.metadata.items():
                    details.append(f"• *{key}*: `{json.dumps(value) if isinstance(value, (dict, list)) else value}`")
            
            # Create action buttons
            keyboard = [
                [
                    InlineKeyboardButton(
                        "⏸️ Pause" if account.is_active else "▶️ Resume",
                        callback_data=f"toggle_account:{account.id}"
                    ),
                    InlineKeyboardButton("✏️ Edit", callback_data=f"edit_account:{account.id}")
                ],
                [
                    InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_account:{account.id}"),
                    InlineKeyboardButton("� Refresh", callback_data=f"account:{account.id}")
                ],
                [
                    InlineKeyboardButton("⬅️ Back to List", callback_data="accounts:1")
                ]
            ]
            
            # Send the formatted message
            await controller._send_text(
                chat_id=update.effective_chat.id,
                text="\n".join(details),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup(keyboard),
                disable_web_page_preview=True
            )
            
    except Exception as exc:
        controller.logger.error(
            "Error showing account details",
            extra={"component": "telegram_handlers", "account_id": account_id, "error": str(exc)},
            exc_info=True
        )
        await controller._safe_notify_error("cmd_account", exc)


async def cmd_add_account(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a new account through interactive conversation."""
    try:
        args = context.args or []
        
        if not args or len(args) < 2:
            # Show account creation help
            help_text = """
            *Add New Account*
            
            Usage: `/add_account <platform> <username> [email] [--key=value ...]`
            
            *Platforms*: `quora`, `reddit`, `youtube`
            
            *Examples*:
            `/add_account quora myusername my@email.com`
            `/add_account reddit myreddituser --is_active=false`
            """
            await controller._send_text(
                update.effective_chat.id,
                help_text,
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
            
        # Parse arguments
        platform_str = args[0].lower()
        username = args[1]
        email = None
        metadata = {}
        
        # Parse additional key-value pairs
        for arg in args[2:]:
            if arg.startswith('--'):
                if '=' in arg:
                    key, value = arg[2:].split('=', 1)
                    metadata[key] = value
            elif not email and '@' in arg and '.' in arg.split('@')[1]:
                email = arg
        
        # Validate platform
        try:
            platform = AccountType(platform_str.upper())
        except ValueError:
            valid_platforms = ', '.join([f'`{e.value}`' for e in AccountType])
            await controller._send_text(
                chat_id=update.effective_chat.id,
                text=f"❌ Invalid platform '{platform_str}'. Valid platforms are: {valid_platforms}",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        
        # Validate email if provided
        if email and not validate_email(email):
            await controller._send_text(
                update.effective_chat.id,
                f"❌ Invalid email format: {email}",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        
        # Create the account
        with controller.db.session_scope() as session:
            account = Account(
                platform=platform.value,
                username=username,
                email=email,
                metadata=metadata or None,
                is_active=True
            )
            session.add(account)
            session.commit()
            
            # Send success message
            await controller._send_text(
                update.effective_chat.id,
                f"✅ Successfully added {platform.value} account: *{username}*\n"
                f"Account ID: `{account.id}`\n"
                f"Use `/account {account.id}` to manage this account.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            
    except Exception as exc:
        controller.logger.error(f"Error adding account: {exc}", exc_info=True)
        await controller._send_text(
            update.effective_chat.id,
            f"❌ Failed to add account: {str(exc)}",
            parse_mode=ParseMode.MARKDOWN_V2
        )


async def cmd_quickreply(controller, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quickly reply to an action with a predefined response.
    
    This allows users to quickly approve an action with a custom response
    without going through the full approval flow.
    
    Args:
        controller: The TelegramController instance
        update: The Telegram update object
        context: The callback context with action_id and response_text as arguments
    """
    try:
        args = context.args or []
        
        # Validate input
        if len(args) < 2:
            await controller._send_text(
                chat_id=update.effective_chat.id,
                text=(
                    "❌ *Invalid usage*\n\n"
                    "Usage: `/quickreply <action_id> <response_text>`\n\n"
                    "Example: `/quickreply abc123 This looks good!`"
                ),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
            
        action_id = args[0]
        response_text = " ".join(args[1:])
        
        # Validate action_id format (alphanumeric with underscores and hyphens)
        if not re.match(r'^[a-zA-Z0-9_-]+$', action_id):
            await controller._send_text(
                chat_id=update.effective_chat.id,
                text="❌ Invalid action ID format. Only alphanumeric characters, underscores and hyphens are allowed.",
                parse_mode=ParseMode.MARKDOWN_V2
            )
            return
        
        # Resolve the action with the quick reply
        result = await controller.resolve_action(
            action_id=action_id,
            response_type="approve",
            response_value=response_text,
            source="quick_reply",
        )
        
        # Send appropriate response
        if result.get('success', False):
            await controller._send_text(
                chat_id=update.effective_chat.id,
                text=(
                    f"✅ *Quick reply sent*\n"
                    f"Action ID: `{action_id}`\n"
                    f"Response: `{sanitize_markdown(response_text[:100])}...`"
                ),
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📋 View Action", callback_data=f"view_action:{action_id}")]
                ])
            )
        else:
            error_msg = result.get('error', 'Unknown error')
            await controller._send_text(
                chat_id=update.effective_chat.id,
                text=(
                    f"❌ *Failed to send quick reply*\n"
                    f"Action ID: `{action_id}`\n"
                    f"Error: `{sanitize_markdown(str(error_msg))}`"
                ),
                parse_mode=ParseMode.MARKDOWN_V2
            )
            
    except Exception as exc:
        controller.logger.error(
            "Error in quickreply command",
            extra={
                "component": "telegram_handlers", 
                "action_id": action_id if 'action_id' in locals() else None,
                "error": str(exc)
            },
            exc_info=True
        )
        await controller._safe_notify_error("cmd_quickreply", exc)
