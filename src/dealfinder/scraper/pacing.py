"""Human-like pacing and a global rate governor.

The rate governor is the single most important ban-avoidance lever: it caps total
scraper *actions* per hour across all identities, independent of how many search
targets fire. Every detail scrape asks the governor for permission first.
"""

from __future__ import annotations

import asyncio
import math
import random
import time

from dealfinder.config import get_settings
from dealfinder.logging import get_logger

log = get_logger(__name__)


class RateGovernor:
    """A token bucket capping total scraper actions/hour across all targets.

    Deliberately lock-free: this is a single-process, approximate limiter, and avoiding
    an ``asyncio.Lock`` lets one instance be shared across event loops (the worker's
    scheduler loop and the web routes' per-request ``asyncio.run`` loops) without the
    "bound to a different event loop" error.
    """

    def __init__(self, max_actions_per_hour: int) -> None:
        self.capacity = float(max(1, max_actions_per_hour))
        self.tokens = self.capacity
        self.refill_per_sec = self.capacity / 3600.0
        self._last = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self._last = now

    async def acquire(self) -> None:
        """Block until a token is available, then consume one."""
        while True:
            self._refill()
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return
            deficit = 1.0 - self.tokens
            wait = deficit / self.refill_per_sec
            log.debug("rate_governor_wait", seconds=round(wait, 1))
            await asyncio.sleep(min(wait, 30.0))


_governor: RateGovernor | None = None


def get_governor() -> RateGovernor:
    """The single process-wide governor — the real global ban-avoidance lever (B4)."""
    global _governor
    if _governor is None:
        _governor = RateGovernor(get_settings().rate_max_actions_per_hour)
    return _governor


async def human_pause() -> None:
    """Sleep a randomized, log-normal-ish think-time between actions."""
    settings = get_settings()
    lo, hi = settings.pacing_min_seconds, settings.pacing_max_seconds
    # Bias toward the low end with an occasional longer pause (log-normal shape).
    mu = math.log((lo + hi) / 2.0)
    delay = min(hi * 1.5, max(lo, random.lognormvariate(mu, 0.4)))
    await asyncio.sleep(delay)


def in_quiet_hours() -> bool:
    """True if the current local hour falls within configured quiet hours."""
    settings = get_settings()
    start, end = settings.quiet_hours_start, settings.quiet_hours_end
    if start is None or end is None:
        return False
    hour = time.localtime().tm_hour
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps past midnight
