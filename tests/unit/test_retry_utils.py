import time

import pytest

from src.utils.retry import RetryError, circuit_breaker, retry_with_exponential_backoff, timeout


def test_retry_with_exponential_backoff_succeeds_on_third_attempt():
    attempts = {"count": 0}

    @retry_with_exponential_backoff(max_attempts=3, base_delay=0.01, max_delay=0.05)
    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise ValueError("fail")
        return "ok"

    assert flaky() == "ok"
    assert attempts["count"] == 3


def test_retry_with_exponential_backoff_fails_after_limit():
    @retry_with_exponential_backoff(max_attempts=2, base_delay=0.01, max_delay=0.02)
    def always_fail():
        raise ValueError("still failing")

    with pytest.raises(RetryError):
        always_fail()


def test_circuit_breaker_opens_after_failures():
    @circuit_breaker(failure_threshold=2, recovery_timeout=0.05, expected_exceptions=(ValueError,))
    def flaky():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        flaky()
    with pytest.raises(ValueError):
        flaky()
    with pytest.raises(RuntimeError):
        flaky()
    time.sleep(0.06)
    with pytest.raises(ValueError):
        flaky()


def test_timeout_decorator_times_out():
    @timeout(seconds=0.05)
    def slow():
        time.sleep(0.2)
        return True

    with pytest.raises(TimeoutError):
        slow()
