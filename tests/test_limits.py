"""The sliding window and its rules (app/api/limits.py), on a clock the test turns."""

from __future__ import annotations

import pytest

from app.api.limits import TooMany, Window, admit, retry_after


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


def test_a_key_hits_up_to_its_limit_then_waits(clock) -> None:
    window = Window(3, 60, clock=clock)
    for _ in range(3):
        admit((window, "a"))
    with pytest.raises(TooMany) as refused:
        admit((window, "a"))
    assert refused.value.retry_after == 60


def test_the_window_slides_rather_than_resets(clock) -> None:
    window = Window(2, 60, clock=clock)
    admit((window, "a"))
    clock.now += 40
    admit((window, "a"))
    clock.now += 21  # the first hit is now out of the window, the second is not
    admit((window, "a"))
    with pytest.raises(TooMany) as refused:
        admit((window, "a"))
    assert refused.value.retry_after == pytest.approx(39)  # the second (t+40) leaves at t+100


def test_keys_are_counted_apart(clock) -> None:
    window = Window(1, 60, clock=clock)
    admit((window, "a"))
    admit((window, "b"))
    with pytest.raises(TooMany):
        admit((window, "a"))


def test_a_refused_hit_is_not_counted(clock) -> None:
    """Hammering while refused does not push the wait further out."""
    window = Window(1, 60, clock=clock)
    admit((window, "a"))
    for _ in range(5):
        clock.now += 10
        with pytest.raises(TooMany):
            admit((window, "a"))
    clock.now += 10.5
    admit((window, "a"))


def test_one_limit_refusing_charges_none_of_the_others(clock) -> None:
    roomy, full = Window(5, 60, clock=clock), Window(1, 60, clock=clock)
    admit((full, "x"))
    for _ in range(3):
        with pytest.raises(TooMany):
            admit((roomy, "a"), (full, "x"))
    assert roomy.wait("a") == 0
    for _ in range(5):
        admit((roomy, "a"))  # all five still there


def test_the_longest_wait_is_the_one_reported(clock) -> None:
    short, long = Window(1, 10, clock=clock), Window(1, 300, clock=clock)
    admit((short, "a"), (long, "a"))
    with pytest.raises(TooMany) as refused:
        admit((short, "a"), (long, "a"))
    assert refused.value.retry_after == 300


def test_stale_keys_are_swept_once_there_are_many(clock) -> None:
    window = Window(1, 60, clock=clock, sweep_above=10)
    for n in range(11):
        admit((window, n))
    clock.now += 61
    window.wait("anything")
    assert len(window._seen) == 0


def test_retry_after_is_whole_seconds_and_never_zero() -> None:
    assert retry_after(0.2) == {"Retry-After": "1"}
    assert retry_after(59.01) == {"Retry-After": "60"}
