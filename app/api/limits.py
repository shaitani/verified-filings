"""Rate limits: every number in one place, and the sliding window that counts them.

Kept in memory, because the Web Server is one process by design (DESIGN §4): a
restart forgets the counts, which only ever lets someone in sooner. The daily
question cap is the exception -- it is counted in the database, where a restart
cannot reset it (``storage.admission``).

Who is asking is ``client_ip``: the address that connected. Behind a reverse
proxy that is the proxy, until the proxy's header is trusted -- the one place to
change when the deployment's front door is chosen (docs/FUTURE.md).
"""

from __future__ import annotations

import math
import time
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field

from fastapi import Request

# --------------------------------------------------------------------------- #
# The numbers (app/api/DESIGN.md §10 for sign-in, §4 for questions)
# --------------------------------------------------------------------------- #

MINUTE = 60.0

#: Sign-in attempts from one IP, across login, registration and GitHub.
AUTH_PER_IP = (30, 15 * MINUTE)
#: Password attempts at one address from one IP: a stranger spends their own budget.
LOGIN_PER_ADDRESS_AND_IP = (10, 15 * MINUTE)
#: Password attempts at one address from browsers it has never signed in on --
#: many IPs guessing one password. A browser it has (``vf_device``) is exempt, so
#: no stranger can lock its owner out of their usual browser.
LOGIN_PER_ADDRESS_NEW_DEVICE = (10, 15 * MINUTE)
#: Password checks across the whole server. Each costs ~50 ms and 64 MB (Argon2),
#: so this bounds what a flood from any number of IPs can take.
LOGIN_ACROSS_SERVER = (5, 1.0)

#: A reader's questions queued or running at once (asking, or answering one back).
UNFINISHED_PER_READER = 2
#: A reader's questions in any 24 hours. Administrators are exempt.
DAILY_PER_READER = 200
DAY = 24 * 3600.0
#: Questions queued or running across the server: past it, the GPU is hours behind.
UNFINISHED_ACROSS_SERVER = 25
#: What a busy server tells the asker to wait, in seconds.
BUSY_RETRY_AFTER = 60

#: Refusals on the wire.
TOO_MANY_ATTEMPTS = "TOO_MANY_ATTEMPTS"
TOO_MANY_QUESTIONS = "TOO_MANY_QUESTIONS"
DAILY_LIMIT = "DAILY_LIMIT"
SERVER_BUSY = "SERVER_BUSY"


# --------------------------------------------------------------------------- #
# Counting
# --------------------------------------------------------------------------- #


@dataclass
class Window:
    """At most ``hits`` per ``seconds`` for each key, over a sliding window."""

    hits: int
    seconds: float
    clock: Callable[[], float] = time.monotonic
    #: Past this many keys, a check sweeps out the ones with nothing left in window,
    #: so a flood of distinct keys cannot grow memory without bound.
    sweep_above: int = 10_000
    _seen: dict[Hashable, deque[float]] = field(default_factory=dict, repr=False)

    def wait(self, key: Hashable) -> float:
        """Seconds until ``key`` may hit again; 0 if it may now. Records nothing."""
        now = self.clock()
        if len(self._seen) > self.sweep_above:
            self._sweep(now)
        times = self._seen.get(key)
        if times is None:
            return 0.0
        while times and times[0] <= now - self.seconds:
            times.popleft()
        if not times:
            del self._seen[key]
            return 0.0
        return times[0] + self.seconds - now if len(times) >= self.hits else 0.0

    def record(self, key: Hashable) -> None:
        self._seen.setdefault(key, deque()).append(self.clock())

    def _sweep(self, now: float) -> None:
        stale = [
            k for k, times in self._seen.items() if not times or times[-1] <= now - self.seconds
        ]
        for key in stale:
            del self._seen[key]


class TooMany(Exception):
    """Over a limit; ``retry_after`` in seconds."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"retry after {retry_after:.0f}s")
        self.retry_after = retry_after


def admit(*checks: tuple[Window, Hashable]) -> None:
    """Every window must allow its key, or none is charged: a hit refused by one
    limit does not use up another's budget. Raises ``TooMany`` with the longest wait.
    No await inside, so two requests cannot interleave between check and record."""
    wait = max(window.wait(key) for window, key in checks)
    if wait > 0:
        raise TooMany(wait)
    for window, key in checks:
        window.record(key)


def retry_after(seconds: float) -> dict[str, str]:
    """The ``Retry-After`` header: whole seconds, rounded up, never 0."""
    return {"Retry-After": str(max(1, math.ceil(seconds)))}


def client_ip(request: Request) -> str:
    """Who is asking. The connecting address -- see the module docstring."""
    return request.client.host if request.client else "unknown"
