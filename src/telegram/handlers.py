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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

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

from src.database.models import Account, AccountType, TelegramInteraction
from src.database.operations import DatabaseSessionManager
from src.utils.rate_limiter import FixedWindowRateLimiter
from src.utils.validators import sanitize_markdown, validate_email, validate_url


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
