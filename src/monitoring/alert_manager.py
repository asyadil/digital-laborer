"""Alert manager for health check events."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List

from src.monitoring.health_checker import HealthCheckResult


@dataclass
class Alert:
    alert_type: str
    severity: str  # info, warning, error, critical
    message: str
    details: Dict[str, Any]
    created_at: datetime
    acknowledged: bool = False


class AlertManager:
    """Process health results and send actionable alerts."""

    def __init__(self, telegram: Any, logger: Any) -> None:
        self.telegram = telegram
        self.logger = logger
        self._active_alerts: List[Alert] = []
        self._rate_limit: Dict[str, datetime] = {}
        self._rate_limit_window = timedelta(minutes=15)
        self._last_status: Dict[str, str] = {}

    async def process_health_results(self, results: Dict[str, HealthCheckResult]) -> None:
        """Generate alerts from health check results."""
        if not results:
            return

        overall = results.get("overall")
        for key, res in results.items():
            if key == "overall":
                continue
            last = self._last_status.get(key)
            if last == res.status:
                continue  # no state change, skip alert
            self._last_status[key] = res.status
            if res.status == "unhealthy":
                await self.send_alert(
                    alert_type=f"{key}_unhealthy",
                    severity="error",
                    message=f"{key} unhealthy",
                    details=res.details,
                )
            elif res.status == "degraded":
                await self.send_alert(
                    alert_type=f"{key}_degraded",
                    severity="warning",
                    message=f"{key} degraded",
                    details=res.details,
                )
        if overall:
            last = self._last_status.get("overall")
            if last != overall.status:
                self._last_status["overall"] = overall.status
                if overall.score < 0.5:
                    await self.send_alert(
                        alert_type="overall_health",
                        severity="critical",
                        message="Overall health below 0.5",
                        details={"score": overall.score},
                    )

    def reset_state(self) -> None:
        """Reset cached state and rate-limit windows (e.g., on restart)."""
        self._last_status.clear()
        self._rate_limit.clear()

    async def send_alert(self, alert_type: str, severity: str, message: str, details: Dict[str, Any]) -> None:
        """Send alert with rate limiting."""
        now = datetime.now(timezone.utc)
        last_sent = self._rate_limit.get(alert_type)
        if last_sent and now - last_sent < self._rate_limit_window:
            return

        alert = Alert(
            alert_type=alert_type,
            severity=severity,
            message=message,
            details=details,
            created_at=now,
        )
        self._active_alerts.append(alert)
        self._rate_limit[alert_type] = now

        icon = {"info": "ℹ️", "warning": "⚠️", "error": "❌", "critical": "🛑"}.get(severity, "ℹ️")
        lines = [
            f"{icon} *{message}*",
            f"*Severity*: {severity}",
            f"*Type*: {alert_type}",
            f"*At*: {now.isoformat()}",
        ]
        for k, v in (details or {}).items():
            lines.append(f"- *{k}*: `{v}`")
        body = "\n".join(lines)

        try:
            if self.telegram:
                await self.telegram.send_notification(body, priority=severity.upper())
        except Exception as exc:  # pragma: no cover - best effort
            self.logger.error(
                "Failed to send alert",
                extra={"component": "alert_manager", "error": str(exc), "alert_type": alert_type},
            )

    def get_active_alerts(self) -> List[Alert]:
        return [a for a in self._active_alerts if not a.acknowledged]

    def acknowledge_alert(self, alert_id: str) -> None:
        for alert in self._active_alerts:
            if alert.alert_type == alert_id and not alert.acknowledged:
                alert.acknowledged = True
                break
