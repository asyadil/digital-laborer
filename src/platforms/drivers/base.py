"""Base protocol for production drivers (real posting flows)."""
from __future__ import annotations

from typing import Protocol, Any, Dict


class PlatformDriver(Protocol):
    """Interface a real automation/API driver should implement."""

    def login(self, account: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def post_comment(self, target_id: str, content: str, account: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def find_target_posts(self, location: str, limit: int = 5) -> Dict[str, Any]:
        ...

    def get_comment_metrics(self, comment_url: str) -> Dict[str, Any]:
        ...

    def check_account_health(self, account: Dict[str, Any]) -> Dict[str, Any]:
        ...
