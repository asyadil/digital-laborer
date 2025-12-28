"""Retry, circuit breaker, and timeout utilities."""
from __future__ import annotations

import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, Optional, Tuple, Type


class RetryError(Exception):
    """Raised when all retry attempts fail."""


_executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="retry_timeout")


def retry_with_exponential_backoff(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exponential_backoff: bool = True,
    retry_exceptions: Tuple[Type[BaseException], ...] = (Exception,),
):
    """Decorator to retry functions with backoff.

    Args:
        max_attempts: Maximum number of attempts including the first.
        base_delay: Initial delay in seconds.
        max_delay: Maximum delay between attempts.
        exponential_backoff: Whether to exponentially increase delay.
        retry_exceptions: Exception types that should trigger a retry.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            attempt = 0
            delay = base_delay
            while True:
                attempt += 1
                try:
                    return func(*args, **kwargs)
                except retry_exceptions as exc:  # type: ignore[misc]
                    if attempt >= max_attempts:
                        raise RetryError(f"Operation failed after {max_attempts} attempts") from exc
                    time.sleep(delay)
                    delay = min(max_delay, delay * 2 if exponential_backoff else delay + base_delay)
        return wrapper

    return decorator


def circuit_breaker(
    failure_threshold: int = 5,
    recovery_timeout: float = 300.0,
    expected_exceptions: Tuple[Type[BaseException], ...] = (Exception,),
):
    """Circuit breaker decorator.

    When failures exceed threshold, circuit opens and calls fail fast until recovery_timeout elapses.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        state = {
            "failures": 0,
            "state": "closed",  # closed, open, half-open
            "opened_at": None,
        }
        lock = threading.Lock()

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with lock:
                now = time.time()
                if state["state"] == "open":
                    if state["opened_at"] and now - state["opened_at"] >= recovery_timeout:
                        state["state"] = "half-open"
                    else:
                        raise RuntimeError("Circuit open: refusing to execute operation")

            try:
                result = func(*args, **kwargs)
            except expected_exceptions:
                with lock:
                    state["failures"] += 1
                    if state["failures"] >= failure_threshold:
                        state["state"] = "open"
                        state["opened_at"] = time.time()
                raise
            else:
                with lock:
                    state["failures"] = 0
                    state["state"] = "closed"
                return result

        return wrapper

    return decorator


def timeout(seconds: float = 30.0, timeout_exception: Type[BaseException] = TimeoutError):
    """Decorator to enforce a timeout using a thread executor."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            future = _executor.submit(func, *args, **kwargs)
            try:
                return future.result(timeout=seconds)
            except FuturesTimeoutError:
                future.cancel()
                raise timeout_exception(f"Operation timed out after {seconds} seconds")

        return wrapper

    return decorator
