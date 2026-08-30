import time

import pytest

from app.ingest.rate_limiter import RateLimiter


@pytest.mark.asyncio
async def test_allows_up_to_the_limit_immediately():
    limiter = RateLimiter(max_per_second=5)
    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed < 0.2  # first 5 should not need to wait


@pytest.mark.asyncio
async def test_throttles_beyond_the_limit():
    limiter = RateLimiter(max_per_second=5)
    start = time.monotonic()
    for _ in range(6):  # one more than the limit
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.9  # the 6th call must wait ~1s for the window to slide


def test_rejects_non_positive_limit():
    with pytest.raises(ValueError):
        RateLimiter(max_per_second=0)
