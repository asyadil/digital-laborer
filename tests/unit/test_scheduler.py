import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.core.scheduler import Scheduler


@pytest.mark.asyncio
async def test_scheduler_runs_once():
    sched = Scheduler()
    stop = asyncio.Event()
    hit = {"count": 0}

    async def job():
        hit["count"] += 1
        stop.set()

    sched.schedule_once("job", datetime.now(timezone.utc) + timedelta(seconds=0.05), lambda: job())
    await sched.run(stop)
    assert hit["count"] == 1


@pytest.mark.asyncio
async def test_scheduler_recurring():
    sched = Scheduler()
    stop = asyncio.Event()
    hit = {"count": 0}

    async def job():
        hit["count"] += 1
        if hit["count"] >= 2:
            stop.set()

    sched.schedule_every("job", interval_seconds=1, start_in_seconds=0, coro_factory=lambda: job())
    await sched.run(stop)
    assert hit["count"] >= 2
