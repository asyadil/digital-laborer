"""Async task scheduling engine."""
from __future__ import annotations

import asyncio
import heapq
import logging
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


@dataclass(order=True)
class _ScheduledItem:
    run_at: float
    seq: int
    name: str = field(compare=False)
    coro_factory: Callable[[], Awaitable[None]] = field(compare=False)
    interval_seconds: Optional[int] = field(default=None, compare=False)


class Scheduler:
    """In-process asyncio scheduler.

    - Supports one-off and recurring tasks
    - Failure isolation: task exceptions are logged and do not crash scheduler
    """

    def __init__(self, logger: Optional[logging.Logger] = None) -> None:
        self.logger = logger or logging.getLogger("scheduler")
        self._heap: list[_ScheduledItem] = []
        self._seq = 0
        self._wake_event = asyncio.Event()
        self._stopping = False
        self._running = False
        self._running_tasks: set[str] = set()

    def schedule_once(self, name: str, when: datetime, coro_factory: Callable[[], Awaitable[None]]) -> None:
        self._push(name=name, when=when, coro_factory=coro_factory, interval_seconds=None)

    def schedule_every(self, name: str, interval_seconds: int, start_in_seconds: int, coro_factory: Callable[[], Awaitable[None]]) -> None:
        when = datetime.now(timezone.utc) + timedelta(seconds=max(0, start_in_seconds))
        self._push(name=name, when=when, coro_factory=coro_factory, interval_seconds=max(1, interval_seconds))

    def _push(self, name: str, when: datetime, coro_factory: Callable[[], Awaitable[None]], interval_seconds: Optional[int]) -> None:
        self._seq += 1
        item = _ScheduledItem(run_at=when.timestamp(), seq=self._seq, name=name, coro_factory=coro_factory, interval_seconds=interval_seconds)
        heapq.heappush(self._heap, item)
        self._wake_event.set()

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run scheduler loop until stop_event is set."""
        self._stopping = False
        self._running = True
        try:
            while not stop_event.is_set() and not self._stopping:
                now = datetime.now(timezone.utc).timestamp()
                item = self._heap[0] if self._heap else None

                if item is None:
                    self._wake_event.clear()
                    await self._wait_any(stop_event)
                    continue

                if item.run_at > now:
                    self._wake_event.clear()
                    timeout = max(0.0, item.run_at - now)
                    await self._wait_any(stop_event, timeout=timeout)
                    continue

                heapq.heappop(self._heap)
                if item.name in self._running_tasks:
                    # Skip scheduling duplicate concurrent run; reschedule next interval if any
                    if item.interval_seconds:
                        next_run = datetime.now(timezone.utc) + timedelta(seconds=item.interval_seconds)
                        self._push(item.name, next_run, item.coro_factory, item.interval_seconds)
                    continue
                self._running_tasks.add(item.name)
                asyncio.create_task(self._run_item(item), name=f"sched:{item.name}")

                if item.interval_seconds:
                    next_run = datetime.now(timezone.utc) + timedelta(seconds=item.interval_seconds)
                    self._push(item.name, next_run, item.coro_factory, item.interval_seconds)

        except asyncio.CancelledError:
            return
        finally:
            self._running = False

    async def _run_item(self, item: _ScheduledItem) -> None:
        start = time.perf_counter()
        success = True
        try:
            await item.coro_factory()
        except Exception as exc:
            success = False
            self.logger.error(
                "Scheduled task failed",
                extra={"component": "scheduler", "task": item.name, "error": str(exc)},
            )
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            self.logger.info(
                "Scheduled task finished",
                extra={
                    "component": "scheduler",
                    "task": item.name,
                    "duration_ms": duration_ms,
                    "success": success,
                },
            )
            self._running_tasks.discard(item.name)

    async def _wait_any(self, stop_event: asyncio.Event, timeout: Optional[float] = None) -> None:
        tasks = [asyncio.create_task(stop_event.wait(), name="sched_stop"), asyncio.create_task(self._wake_event.wait(), name="sched_wake")]
        try:
            done, pending = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

    def stop(self) -> None:
        self._stopping = True
        self._wake_event.set()

    def is_running(self) -> bool:
        return self._running and not self._stopping
