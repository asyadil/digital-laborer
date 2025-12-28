"""Input validation helpers."""
from __future__ import annotations

import re
from typing import Iterable


class ValidationError(ValueError):
    """Raised when validation fails."""


def validate_non_empty_str(value: str, field_name: str) -> str:
    if value is None or not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def validate_choices(value: str, choices: Iterable[str], field_name: str) -> str:
    if value not in choices:
        raise ValidationError(f"{field_name} must be one of {', '.join(choices)}")
    return value


def validate_email(email: str) -> bool:
    """Validate email format.
    
    Args:
        email: Email address to validate
        
    Returns:
        bool: True if email is valid, False otherwise
    """
    if not email:
        return False
    pattern = r'^[a-z0-9]+[\w.-]*@[a-z0-9]+[\w-]*(\.[a-z0-9]+[\w-]*)+$'
    return bool(re.fullmatch(pattern, email.lower()))


def validate_url(url: str) -> bool:
    """Validate URL format.
    
    Args:
        url: URL to validate
        
    Returns:
        bool: True if URL is valid, False otherwise
    """
    if not url:
        return False
    pattern = r'^https?://[\w.-]+(?:\.[\w-]+)+[\w\-._~:/?#[\]@!$&\'()*+,;=]*$'
    return bool(re.match(pattern, url, re.IGNORECASE))


def sanitize_markdown(text: str) -> str:
    """Escape Telegram markdown special characters to avoid injection."""
    if text is None:
        return ""
    return re.sub(r"([_`*\[\]()~>#+\-=|{}.!])", r"\\\1", text)
