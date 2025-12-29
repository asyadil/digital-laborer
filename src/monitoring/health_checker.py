"""System health checking utilities."""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psutil  # type: ignore

from src.database.models import Account, AccountStatus, SystemMetric, Post
from src.database.operations import DatabaseSessionManager


@dataclass
class HealthCheckResult:
    component: str
    status: str  # healthy, degraded, unhealthy
    score: float  # 0.0 - 1.0
    details: Dict[str, Any]
    checked_at: datetime
    error: Optional[str] = None


class HealthChecker:
    """Runs concurrent health checks across subsystems."""

    def __init__(self, db: DatabaseSessionManager, telegram: Any, logger: Any) -> None:
        self.db = db
        self.telegram = telegram
        self.logger = logger

    async def check_all(self) -> Dict[str, HealthCheckResult]:
        """Run all checks concurrently and compute overall health."""
        tasks = {
            "database": asyncio.create_task(self.check_database()),
            "telegram": asyncio.create_task(self.check_telegram()),
            "disk": asyncio.create_task(self.check_disk_space()),
            "memory": asyncio.create_task(self.check_memory()),
            "platforms": asyncio.create_task(self.check_platform_adapters()),
        }
        results: Dict[str, HealthCheckResult] = {}
        for key, task in tasks.items():
            try:
                results[key] = await task
            except Exception as exc:  # pragma: no cover - safety net
                self.logger.error(
                    "Health check failed",
                    extra={"component": "health_checker", "check": key, "error": str(exc)},
                )
                results[key] = HealthCheckResult(
                    component=key,
                    status="unhealthy",
                    score=0.0,
                    details={},
                    checked_at=datetime.now(timezone.utc),
                    error=str(exc),
                )

        overall = self._calculate_overall_health(results)
        results["overall"] = overall
        return results

    async def check_database(self) -> HealthCheckResult:
        """Check DB connectivity and simple responsiveness."""
        started = time.perf_counter()
        details: Dict[str, Any] = {}
        status = "healthy"
        error: Optional[str] = None
        score = 1.0

        try:
            with self.db.session_scope() as session:
                # lightweight count to validate connectivity
                post_count = session.query(Post.id).limit(1).count()
                details["sample_posts"] = post_count
        except Exception as exc:
            status = "unhealthy"
            score = 0.0
            error = str(exc)
        else:
            elapsed_ms = (time.perf_counter() - started) * 1000
            details["response_ms"] = elapsed_ms
            if elapsed_ms > 500:
                status = "degraded"
                score = 0.6
            if elapsed_ms > 1500:
                status = "unhealthy"
                score = 0.3

        return HealthCheckResult(
            component="database",
            status=status,
            score=max(0.0, min(1.0, score)),
            details=details,
            checked_at=datetime.now(timezone.utc),
            error=error,
        )

    async def check_telegram(self) -> HealthCheckResult:
        """Check Telegram controller readiness."""
        status = "healthy"
        score = 1.0
        error: Optional[str] = None
        details: Dict[str, Any] = {}
        try:
            pending = int(getattr(self.telegram, "pending_actions_count", 0)) if self.telegram else 0
            details["pending_actions"] = pending
            if pending > 10:
                status = "degraded"
                score = 0.6
        except Exception as exc:
            status = "unhealthy"
            score = 0.0
            error = str(exc)
        return HealthCheckResult(
            component="telegram",
            status=status,
            score=max(0.0, min(1.0, score)),
            details=details,
            checked_at=datetime.now(timezone.utc),
            error=error,
        )

    async def check_disk_space(self) -> HealthCheckResult:
        """Check disk usage thresholds."""
        usage = psutil.disk_usage("/")  # type: ignore[arg-type]
        percent = usage.percent
        free_mb = usage.free / (1024 * 1024)
        status = "healthy"
        score = 1.0
        if percent > 90 or free_mb < 500:
            status = "unhealthy"
            score = 0.2
        elif percent > 80 or free_mb < 1024:
            status = "degraded"
            score = 0.6
        return HealthCheckResult(
            component="disk",
            status=status,
            score=score,
            details={"percent_used": percent, "free_mb": free_mb},
            checked_at=datetime.now(timezone.utc),
        )

    async def check_memory(self) -> HealthCheckResult:
        """Check system and process memory usage."""
        vm = psutil.virtual_memory()
        status = "healthy"
        score = 1.0
        free_mb = vm.available / (1024 * 1024)
        percent = vm.percent
        process_rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)

        if percent > 90 or free_mb < 500:
            status = "unhealthy"
            score = 0.2
        elif percent > 80 or free_mb < 1024:
            status = "degraded"
            score = 0.6

        return HealthCheckResult(
            component="memory",
            status=status,
            score=score,
            details={
                "percent_used": percent,
                "free_mb": free_mb,
                "process_rss_mb": process_rss_mb,
            },
            checked_at=datetime.now(timezone.utc),
        )

    async def check_platform_adapters(self) -> HealthCheckResult:
        """Evaluate account health scores from the database."""
        status = "healthy"
        score = 1.0
        details: Dict[str, Any] = {}
        try:
            with self.db.session_scope() as session:
                active_accounts = session.query(Account).filter(Account.status == AccountStatus.active).all()
                flagged_accounts = session.query(Account).filter(Account.status == AccountStatus.flagged).all()
                if not active_accounts:
                    status = "degraded"
                    score = 0.6
                    details["note"] = "No active accounts"
                else:
                    avg_health = sum(a.health_score or 0.0 for a in active_accounts) / max(1, len(active_accounts))
                    details["average_health"] = avg_health
                    details["active_accounts"] = len(active_accounts)
                    details["flagged_accounts"] = len(flagged_accounts)
                    if avg_health < 0.6:
                        status = "degraded"
                        score = 0.6
                    if avg_health < 0.4:
                        status = "unhealthy"
                        score = 0.3
        except Exception as exc:
            status = "unhealthy"
            score = 0.0
            details["error"] = str(exc)

        return HealthCheckResult(
            component="platforms",
            status=status,
            score=max(0.0, min(1.0, score)),
            details=details,
            checked_at=datetime.now(timezone.utc),
            error=details.get("error"),
        )

    def _calculate_overall_health(self, results: Dict[str, HealthCheckResult]) -> HealthCheckResult:
        weights = {
            "database": 0.30,
            "telegram": 0.25,
            "platforms": 0.25,
            "disk": 0.10,
            "memory": 0.10,
        }
        overall_score = 0.0
        for component, weight in weights.items():
            comp_score = results.get(component).score if component in results else 0.0
            overall_score += comp_score * weight

        status = "healthy"
        if overall_score < 0.5:
            status = "unhealthy"
        elif overall_score < 0.75:
            status = "degraded"

        return HealthCheckResult(
            component="overall",
            status=status,
            score=round(overall_score, 3),
            details={c: r.score for c, r in results.items()},
            checked_at=datetime.now(timezone.utc),
        )
