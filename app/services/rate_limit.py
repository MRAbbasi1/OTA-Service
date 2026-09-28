"""Rate limiting for the device and administrative surfaces.

Needed independently of the device-side 24-hour lockout, which is RAM-only and
cleared by a reboot, so it protects nothing against a device that keeps
restarting (``docs/OtaManager.md`` §4.1).

The response is always ``429``, never ``403``: a 429 is a non-authentication 4xx,
so the device treats it as a non-retryable response for the current poll and
tries again at its normal interval. It does **not** enter the 24-hour lockout
path, which is why a limited poll is a skipped cycle rather than a silenced
device.

Limits are keyed by authenticated device identity, the only key a device cannot
forge without credentials, and they are deliberately generous enough for the
documented retry sequence (5 attempts with 5/10/20/40/80 second backoff).

The initial single-instance deployment keeps window state in process memory.
Scaling to multiple API replicas needs a shared store, which is a deliberate
architecture change rather than an implicit dependency
(``docs/15-implementation-decisions.md`` §7), so the limiter is an injectable
object instead of a module-level singleton.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

MANIFEST_SURFACE = "manifest"
FIRMWARE_SURFACE = "firmware"
ADMIN_LOGIN_SURFACE = "admin_login"
ADMIN_API_SURFACE = "admin_api"

_SWEEP_THRESHOLD = 10_000


@dataclass(frozen=True)
class RateLimit:
    limit: int
    window_seconds: int


# Documented initial limits (docs/15-implementation-decisions.md §7). A device
# following the firmware's retry sequence makes at most about six manifest
# requests in three minutes, so both windows leave room for normal behaviour
# plus a retry burst, and never limit a healthy fleet.
MANIFEST_RATE_LIMIT = RateLimit(limit=20, window_seconds=300)
FIRMWARE_RATE_LIMIT = RateLimit(limit=10, window_seconds=900)

# Administrative surfaces. Login is keyed by source address *and* the submitted
# email, so one noisy address cannot lock a legitimate operator out of their own
# account, and credential stuffing against many accounts from one address is
# still bounded. The general admin limit is keyed by authenticated account, so a
# shared office address is not a shared quota.
ADMIN_LOGIN_RATE_LIMIT = RateLimit(limit=5, window_seconds=900)
ADMIN_API_RATE_LIMIT = RateLimit(limit=120, window_seconds=60)


class InMemoryRateLimiter:
    """Fixed window per key, counting from the first request in that window.

    Safe for concurrent use from the request thread pool. State is pruned on
    access once the map grows past `_SWEEP_THRESHOLD`, so a long-lived process
    does not accumulate entries for devices that stopped polling.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, limit: RateLimit) -> bool:
        now = self._clock()
        with self._lock:
            if len(self._windows) > _SWEEP_THRESHOLD:
                self._sweep(now)
            previous = self._windows.get(key)
            if previous is None or now >= previous[0]:
                self._windows[key] = (now + limit.window_seconds, 1)
                return True
            expires_at, count = previous
            count += 1
            self._windows[key] = (expires_at, count)
            return count <= limit.limit

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()

    def _sweep(self, now: float) -> None:
        expired = [key for key, (expires_at, _) in self._windows.items() if expires_at <= now]
        for key in expired:
            del self._windows[key]
