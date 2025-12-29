"""Analytics aggregation and metric recording."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, and_

from src.database.models import Post, SystemMetric, Account
from src.database.operations import DatabaseSessionManager


@dataclass
class PerformanceMetrics:
    period_start: datetime
    period_end: datetime
    total_posts: int
    posts_by_platform: Dict[str, int]
    total_clicks: int
    total_conversions: int
    conversion_rate: float
    avg_quality_score: float
    top_performing_posts: List[Dict[str, Any]]
    account_performance: Dict[str, Dict[str, Any]]


class Analytics:
    """Compute and persist system performance metrics."""

    def __init__(self, db: DatabaseSessionManager, logger: Any) -> None:
        self.db = db
        self.logger = logger

    def record_metric(self, metric_type: str, value: float, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Store a metric sample."""
        try:
            with self.db.session_scope() as session:
                session.add(
                    SystemMetric(
                        timestamp=datetime.now(timezone.utc),
                        metric_type=metric_type,
                        value=value,
                        metadata_json=metadata or {},
                    )
                )
        except Exception as exc:
            self.logger.error(
                "Failed to record metric",
                extra={"component": "analytics", "metric_type": metric_type, "error": str(exc)},
            )

    def get_metrics(self, start_date: datetime, end_date: datetime) -> PerformanceMetrics:
        """Aggregate metrics between two datetimes."""
        with self.db.session_scope() as session:
            posts_q = (
                session.query(Post)
                .filter(Post.created_at >= start_date)
                .filter(Post.created_at <= end_date)
            )
            total_posts = posts_q.count()

            posts_by_platform_rows = (
                session.query(Post.platform, func.count(Post.id))
                .filter(Post.created_at >= start_date, Post.created_at <= end_date)
                .group_by(Post.platform)
                .all()
            )
            posts_by_platform = {row[0]: int(row[1]) for row in posts_by_platform_rows}

            clicks_sum = (
                session.query(func.coalesce(func.sum(Post.clicks), 0))
                .filter(Post.created_at >= start_date, Post.created_at <= end_date)
                .scalar()
                or 0
            )
            conversions_sum = (
                session.query(func.coalesce(func.sum(Post.conversions), 0))
                .filter(Post.created_at >= start_date, Post.created_at <= end_date)
                .scalar()
                or 0
            )
            conversion_rate = 0.0
            if clicks_sum > 0:
                conversion_rate = conversions_sum / clicks_sum

            avg_quality = (
                session.query(func.coalesce(func.avg(Post.quality_score), 0.0))
                .filter(Post.created_at >= start_date, Post.created_at <= end_date)
                .scalar()
                or 0.0
            )

            top_posts_rows = (
                session.query(Post)
                .filter(Post.created_at >= start_date, Post.created_at <= end_date)
                .order_by(Post.conversions.desc(), Post.clicks.desc())
                .limit(5)
                .all()
            )
            top_performing_posts = [
                {
                    "id": p.id,
                    "platform": p.platform,
                    "clicks": p.clicks,
                    "conversions": p.conversions,
                    "quality_score": p.quality_score,
                    "url": p.url,
                }
                for p in top_posts_rows
            ]

            account_rows = (
                session.query(Account.platform, func.count(Account.id), func.avg(Account.health_score))
                .filter(Account.status == Account.status.active)
                .group_by(Account.platform)
                .all()
            )
            account_performance: Dict[str, Dict[str, Any]] = {}
            for platform, count, avg_health in account_rows:
                account_performance[platform.value if hasattr(platform, "value") else platform] = {
                    "active_accounts": int(count),
                    "avg_health_score": float(avg_health or 0.0),
                }

        return PerformanceMetrics(
            period_start=start_date,
            period_end=end_date,
            total_posts=total_posts,
            posts_by_platform=posts_by_platform,
            total_clicks=int(clicks_sum),
            total_conversions=int(conversions_sum),
            conversion_rate=round(conversion_rate, 4),
            avg_quality_score=float(avg_quality or 0.0),
            top_performing_posts=top_performing_posts,
            account_performance=account_performance,
        )

    def get_platform_breakdown(self) -> Dict[str, Dict[str, Any]]:
        """Return simple platform breakdown using latest metrics."""
        with self.db.session_scope() as session:
            rows = (
                session.query(SystemMetric.metric_type, SystemMetric.value, SystemMetric.metadata_json)
                .order_by(SystemMetric.timestamp.desc())
                .limit(100)
                .all()
            )
        breakdown: Dict[str, Dict[str, Any]] = {}
        for metric_type, value, meta in rows:
            platform = (meta or {}).get("platform") or "general"
            breakdown.setdefault(platform, {"metrics": {}})
            breakdown[platform]["metrics"][metric_type] = value
        return breakdown

    def get_account_performance(self) -> Dict[str, Dict]:
        """Summaries per platform."""
        with self.db.session_scope() as session:
            rows = (
                session.query(Account.platform, func.count(Account.id), func.avg(Account.health_score))
                .filter(Account.status == Account.status.active)
                .group_by(Account.platform)
                .all()
            )
        data: Dict[str, Dict[str, Any]] = {}
        for platform, count, avg_health in rows:
            key = platform.value if hasattr(platform, "value") else platform
            data[key] = {
                "active_accounts": int(count),
                "avg_health_score": float(avg_health or 0.0),
            }
        return data
