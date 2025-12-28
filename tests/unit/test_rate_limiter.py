import time

from src.utils.rate_limiter import FixedWindowRateLimiter, TokenBucketRateLimiter


def test_token_bucket_try_acquire():
    limiter = TokenBucketRateLimiter(rate=1.0, capacity=1)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    time.sleep(1.1)
    assert limiter.try_acquire() is True


def test_fixed_window_rate_limiter():
    limiter = FixedWindowRateLimiter(max_calls=2, window_seconds=0.2)
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False
    time.sleep(0.25)
    assert limiter.try_acquire() is True
