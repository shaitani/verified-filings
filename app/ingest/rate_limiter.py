"""Global async rate limiter shared by every SEC HTTP request.

A single `RateLimiter` instance is shared across all `SECClient` objects
in the process (see `sec_client.py`), so the cap holds no matter how many
clients exist or how many requests are in flight concurrently.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


class RateLimiter:
    """Enforces "no more than `max_per_second` acquisitions complete within
    any rolling 1-second window", across all concurrent callers.

    This is a leaky-bucket style limiter, not a fixed-window counter: the
    window slides continuously rather than resetting on the second, so it
    can't burst up to 2x the limit at a window boundary.
    """

    def __init__(self, max_per_second: int) -> None:
        if max_per_second <= 0:
            raise ValueError("max_per_second must be positive")
        self._max_per_second = max_per_second
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Block until it is safe to issue one more request without
        exceeding the global rate limit, then record that a request is
        being made now."""
        while True:
            async with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 1.0:
                    self._timestamps.popleft()

                if len(self._timestamps) < self._max_per_second:
                    self._timestamps.append(now)
                    return

                sleep_for = 1.0 - (now - self._timestamps[0])

            # Sleep outside the lock so other waiters can re-check too.
            await asyncio.sleep(max(sleep_for, 0.0))
