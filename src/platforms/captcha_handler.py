"""Human-in-the-loop CAPTCHA and challenge handler."""
from __future__ import annotations

import asyncio
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from selenium.webdriver.remote.webdriver import WebDriver
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from src.telegram.controller import TelegramController


@dataclass
class ChallengeResult:
    action_id: str
    solved: bool
    response: Optional[str]
    timeout: bool
    error: Optional[str] = None
    responded_at: Optional[datetime] = None


class CaptchaHandler:
    """Coordinate CAPTCHA/2FA/email verification flows via Telegram."""

    def __init__(self, telegram_controller: TelegramController) -> None:
        self.telegram = telegram_controller
        if self.telegram is None:
            raise ValueError("CaptchaHandler requires a TelegramController instance")

    async def handle_captcha(
        self,
        driver: WebDriver,
        challenge_type: str,
        context: Dict[str, Any],
        timeout: int = 600,
    ) -> ChallengeResult:
        """Handle CAPTCHA challenge by sending screenshot and awaiting human solve."""
        action_id = uuid.uuid4().hex
        screenshot_path = self._take_screenshot(driver)
        try:
            action_context = {
                **(context or {}),
                "action_id": action_id,
                "challenge_type": challenge_type,
                "screenshot_path": screenshot_path,
                "instructions": "Reply with the CAPTCHA solution text. Use buttons to skip or refresh.",
            }
            message = self._format_message("CAPTCHA detected", action_context)
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Solved", callback_data=f"action:{action_id}:solved"),
                        InlineKeyboardButton("🔁 Refresh Screenshot", callback_data=f"action:{action_id}:refresh"),
                    ],
                    [
                        InlineKeyboardButton("⏭️ Skip", callback_data=f"action:{action_id}:skip"),
                    ],
                ]
            )

            await self.telegram.send_file(
                chat_id=self.telegram.user_chat_id,
                file_path=screenshot_path,
                caption="📸 CAPTCHA screenshot",
            )

            result = await self.telegram.request_custom_input(
                action_type="CAPTCHA_SOLVE",
                context=action_context,
                message=message,
                reply_markup=keyboard,
                timeout=timeout,
                action_id=action_id,
            )

            # Handle refresh loop
            if result.response_value == "refresh":
                # Recurse once with a fresh screenshot
                return await self.handle_captcha(driver, challenge_type, context, timeout)

            solved = bool(result.response_value and result.response_value not in {"skip", "solved"})
            if result.response_value == "solved":
                solved = True

            return ChallengeResult(
                action_id=result.action_id,
                solved=solved,
                response=result.response_value,
                timeout=result.timeout,
                responded_at=result.responded_at,
            )
        finally:
            self._cleanup_file(screenshot_path)

    async def handle_2fa(self, platform: str, method: str, timeout: int = 600) -> ChallengeResult:
        """Request 2FA code via Telegram."""
        context = {
            "platform": platform,
            "method": method,
            "instructions": f"Enter the {method} 2FA code for {platform}.",
        }
        message = self._format_message("2FA required", context)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("🔐 Enter Code", callback_data="action:{action_id}:enter_code"),
                    InlineKeyboardButton("⏭️ Skip", callback_data="action:{action_id}:skip"),
                ]
            ]
        )
        result = await self.telegram.request_custom_input(
            action_type="2FA_CODE",
            context=context,
            message=message,
            reply_markup=keyboard,
            timeout=timeout,
        )
        return ChallengeResult(
            action_id=result.action_id,
            solved=bool(result.response_value and result.response_value != "skip"),
            response=result.response_value,
            timeout=result.timeout,
            responded_at=result.responded_at,
        )

    # Synchronous helpers for adapters that are synchronous
    def handle_captcha_sync(
        self,
        driver: WebDriver,
        challenge_type: str,
        context: Dict[str, Any],
        timeout: int = 600,
    ) -> ChallengeResult:
        return self._run_sync(self.handle_captcha(driver, challenge_type, context, timeout))

    def handle_2fa_sync(self, platform: str, method: str, timeout: int = 600) -> ChallengeResult:
        return self._run_sync(self.handle_2fa(platform, method, timeout))

    def handle_verification_email_sync(self, platform: str, timeout: int = 900) -> ChallengeResult:
        return self._run_sync(self.handle_verification_email(platform, timeout))

    def _run_sync(self, coro):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        else:
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result()

    async def handle_verification_email(self, platform: str, timeout: int = 900) -> ChallengeResult:
        """Request email verification code via Telegram."""
        context = {
            "platform": platform,
            "instructions": f"Enter the email verification code received for {platform}.",
        }
        message = self._format_message("Email verification required", context)
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✉️ Enter Code", callback_data="action:{action_id}:enter_code"),
                    InlineKeyboardButton("⏭️ Skip", callback_data="action:{action_id}:skip"),
                ]
            ]
        )
        result = await self.telegram.request_custom_input(
            action_type="EMAIL_VERIFICATION",
            context=context,
            message=message,
            reply_markup=keyboard,
            timeout=timeout,
        )
        return ChallengeResult(
            action_id=result.action_id,
            solved=bool(result.response_value and result.response_value != "skip"),
            response=result.response_value,
            timeout=result.timeout,
            responded_at=result.responded_at,
        )

    def _take_screenshot(self, driver: WebDriver) -> str:
        os.makedirs("data/screenshots", exist_ok=True)
        fd, path = tempfile.mkstemp(prefix="captcha_", suffix=".png", dir="data/screenshots")
        os.close(fd)
        driver.save_screenshot(path)
        return path

    def _cleanup_file(self, path: str) -> None:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            # Best-effort cleanup
            pass

    def _format_message(self, title: str, context: Dict[str, Any]) -> str:
        lines = [f"*{title}*", ""]
        for key, val in context.items():
            lines.append(f"- *{key}*: `{val}`")
        lines.append("")
        lines.append("Reply with the solution text or use buttons.")
        return "\n".join(lines)
