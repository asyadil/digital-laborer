"""Thread-safe rate limiter utilities."""
from __future__ import annotations

import threading
import time
from typing import Optional


class TokenBucketRateLimiter:
    """Simple token bucket rate limiter.

    Provides try_acquire to check allowance and acquire to block until allowed.
    """

    def __init__(self, rate: float, capacity: int, clock: Optional[callable] = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.timestamp = (clock or time.monotonic)()
        self.clock = clock or time.monotonic
        self.lock = threading.Lock()

    def _add_new_tokens(self) -> None:
        now = self.clock()
        elapsed = now - self.timestamp
        added = elapsed * self.rate
        if added > 0:
            self.tokens = min(self.capacity, self.tokens + added)
            self.timestamp = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        if tokens <= 0:
            return True
        with self.lock:
            self._add_new_tokens()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

    def acquire(self, tokens: float = 1.0, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else self.clock() + timeout
        while True:
            with self.lock:
                self._add_new_tokens()
                if self.tokens >= tokens:
                    self.tokens -= tokens
                    return True
                wait_time = (tokens - self.tokens) / self.rate
            if deadline is not None:
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return False
                wait_time = min(wait_time, max(0.0, remaining))
            time.sleep(wait_time)


class FixedWindowRateLimiter:
    """Fixed window rate limiter suitable for coarse-grained enforcement."""

    def __init__(self, max_calls: int, window_seconds: float, clock: Optional[callable] = None) -> None:
        if max_calls <= 0:
            raise ValueError("max_calls must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self.clock = clock or time.time
        self.lock = threading.Lock()
        self.window_start = self.clock()
        self.calls = 0

    def try_acquire(self) -> bool:
        with self.lock:
            now = self.clock()
            if now - self.window_start >= self.window_seconds:
                self.window_start = now
                self.calls = 0
            if self.calls < self.max_calls:
                self.calls += 1
                return True
            return False

    def acquire(self, timeout: Optional[float] = None, sleep_interval: float = 0.05) -> bool:
        deadline = None if timeout is None else self.clock() + timeout
        while True:
            if self.try_acquire():
                return True
            if deadline is not None and self.clock() >= deadline:
                return False
            time.sleep(sleep_interval)
    
    def __enter__(self):
        """Context manager entry - acquire a token."""
        self.acquire()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - nothing to clean up."""
        pass
