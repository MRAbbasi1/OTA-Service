from __future__ import annotations

from app.services.rate_limit import (
    FIRMWARE_RATE_LIMIT,
    MANIFEST_RATE_LIMIT,
    InMemoryRateLimiter,
    RateLimit,
)

LIMIT = RateLimit(limit=3, window_seconds=60)


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_allows_up_to_the_limit_then_refuses() -> None:
    limiter = InMemoryRateLimiter(FakeClock())
    assert [limiter.allow("k", LIMIT) for _ in range(3)] == [True, True, True]
    assert limiter.allow("k", LIMIT) is False


def test_window_expiry_restores_the_allowance() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock)
    for _ in range(4):
        limiter.allow("k", LIMIT)
    assert limiter.allow("k", LIMIT) is False
    clock.advance(59)
    assert limiter.allow("k", LIMIT) is False
    clock.advance(1)
    assert limiter.allow("k", LIMIT) is True


def test_keys_are_independent() -> None:
    limiter = InMemoryRateLimiter(FakeClock())
    for _ in range(4):
        limiter.allow("first", LIMIT)
    assert limiter.allow("first", LIMIT) is False
    assert limiter.allow("second", LIMIT) is True


def test_a_reset_clears_state() -> None:
    limiter = InMemoryRateLimiter(FakeClock())
    for _ in range(4):
        limiter.allow("k", LIMIT)
    limiter.reset()
    assert limiter.allow("k", LIMIT) is True


def test_expired_keys_are_swept_out() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(clock)
    for index in range(10_001):
        limiter.allow(f"k{index}", LIMIT)
    clock.advance(61)
    assert limiter.allow("fresh", LIMIT) is True
    assert len(limiter._windows) < 10_001


def test_documented_limits_leave_room_for_the_firmware_retry_sequence() -> None:
    """The limits are locked to the documented figures (docs/15 §7).

    The firmware retries a failed download up to five times with 5/10/20/40/80
    second backoff, and otherwise polls on its own interval, so a limit that
    tripped under normal behaviour would silence healthy devices.
    """
    assert (MANIFEST_RATE_LIMIT.limit, MANIFEST_RATE_LIMIT.window_seconds) == (20, 300)
    assert (FIRMWARE_RATE_LIMIT.limit, FIRMWARE_RATE_LIMIT.window_seconds) == (10, 900)
    assert MANIFEST_RATE_LIMIT.limit > 6  # one poll plus a five-attempt retry burst
