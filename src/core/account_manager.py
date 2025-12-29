"""Account selection, rotation, and health management."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from src.database.models import Account, AccountStatus
from src.database.operations import DatabaseSessionManager


class AccountManager:
    """Select and rotate platform accounts based on health and recency."""

    def __init__(self, db: DatabaseSessionManager, logger: Any) -> None:
        self.db = db
        self.logger = logger
        self.exclusion_window_hours = 6

    def get_best_account(self, platform: str, criteria: Optional[Dict[str, Any]] = None) -> Optional[Account]:
        """Return the healthiest, least-recently-used active account."""
        criteria = criteria or {}
        exclude_recent = datetime.now(timezone.utc) - timedelta(hours=self.exclusion_window_hours)
        with self.db.session_scope(logger=self.logger) as session:
            query = (
                session.query(Account)
                .filter(Account.platform == platform)
                .filter(Account.status == AccountStatus.active)
                .filter((Account.last_used.is_(None)) | (Account.last_used < exclude_recent))
            )
            account = (
                query.order_by(Account.health_score.desc(), Account.last_used.asc().nullsfirst())
                .first()
            )
            if account:
                return account
        return None

    def rotate_accounts(self, platform: str) -> Optional[Account]:
        """Disable current best if unhealthy and pick next best."""
        with self.db.session_scope(logger=self.logger) as session:
            current = (
                session.query(Account)
                .filter(Account.platform == platform, Account.status == AccountStatus.active)
                .order_by(Account.last_used.desc().nullsfirst())
                .first()
            )
            if current and (current.health_score or 0) < 0.6:
                current.status = AccountStatus.flagged
                session.add(current)
                self.logger.info(
                    "Rotated account due to low health",
                    extra={"platform": platform, "account_id": current.id, "health": current.health_score},
                )
        return self.get_best_account(platform)

    def check_all_account_health(self) -> Dict[int, float]:
        """Return mapping of account_id -> health_score."""
        scores: Dict[int, float] = {}
        with self.db.session_scope(logger=self.logger) as session:
            for acc in session.query(Account).all():
                scores[acc.id] = acc.health_score or 0.0
        return scores

    def disable_unhealthy_accounts(self, threshold: float = 0.3) -> List[int]:
        disabled: List[int] = []
        with self.db.session_scope(logger=self.logger) as session:
            accounts = session.query(Account).filter(Account.health_score < threshold).all()
            for acc in accounts:
                if acc.status != AccountStatus.flagged:
                    acc.status = AccountStatus.flagged
                    disabled.append(acc.id)
        if disabled:
            self.logger.warning(
                "Disabled unhealthy accounts",
                extra={"accounts": disabled, "threshold": threshold},
            )
        return disabled

    def reactivate_recovered_accounts(self) -> List[int]:
        """Reactivate accounts flagged for rate limits after cooldown."""
        reactivated: List[int] = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        with self.db.session_scope(logger=self.logger) as session:
            accounts = (
                session.query(Account)
                .filter(Account.status == AccountStatus.flagged)
                .filter(Account.last_used < cutoff)
                .all()
            )
            for acc in accounts:
                if (acc.health_score or 0) >= 0.3:
                    acc.status = AccountStatus.active
                    reactivated.append(acc.id)
        if reactivated:
            self.logger.info(
                "Reactivated accounts after cooldown",
                extra={"accounts": reactivated},
            )
        return reactivated
