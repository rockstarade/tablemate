"""Precision timing for snipe execution.

Performance optimizations:
- Non-blocking NTP via asyncio.to_thread (saves 2-5s at startup)
- Resy clock calibration support (adjusts pre-fire by server offset)
- Two-phase timing: coarse asyncio.sleep + busy-wait spin for sub-ms precision
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tablement.models import SnipeConfig

logger = logging.getLogger(__name__)


class PrecisionScheduler:
    """
    Handles timing for snipe execution.

    Two-phase approach:
    1. asyncio.sleep() until T-2s (efficient, no CPU burn)
    2. Busy-wait on time.monotonic() for sub-ms precision

    Clock compensation:
    - NTP offset (system clock vs real time)
    - Resy server clock offset (their clock vs ours)
    """

    def __init__(self) -> None:
        self._ntp_offset: float | None = None
        self._resy_offset: float = 0.0

    @property
    def total_offset(self) -> float:
        """Combined clock offset in seconds (NTP + Resy)."""
        ntp = self._ntp_offset or 0.0
        return ntp + self._resy_offset

    def calculate_drop_datetime(self, config: SnipeConfig) -> datetime:
        """
        Compute the exact moment reservations become available.

        Example: date=2026-03-26, days_ahead=30, hour=10, tz=ET
        → drop at 2026-02-24 10:00:00 ET
        """
        tz = ZoneInfo(config.drop_time.timezone)
        drop_date = config.date - timedelta(days=config.drop_time.days_ahead)
        return datetime(
            year=drop_date.year,
            month=drop_date.month,
            day=drop_date.day,
            hour=config.drop_time.hour,
            minute=config.drop_time.minute,
            second=config.drop_time.second,
            tzinfo=tz,
        )

    async def wait_until(
        self, target: datetime, pre_wake_seconds: float = 2.0
    ) -> None:
        """Sleep until target time, then busy-wait for precision."""
        now = datetime.now(target.tzinfo)
        total_wait = (target - now).total_seconds()

        if total_wait <= 0:
            logger.info("Target time already passed (%.1fs ago)", -total_wait)
            return

        logger.info("Waiting %.1fs until %s", total_wait, target.isoformat())

        # Phase 1: Coarse sleep (efficient)
        coarse_sleep = max(0, total_wait - pre_wake_seconds)
        if coarse_sleep > 0:
            await asyncio.sleep(coarse_sleep)

        # Phase 2: Busy-wait for sub-ms precision
        remaining = (target - datetime.now(target.tzinfo)).total_seconds()
        if remaining <= 0:
            return

        target_mono = time.monotonic() + remaining
        while time.monotonic() < target_mono:
            pass  # Tight spin for precision

    @staticmethod
    def _now(tz) -> datetime:
        """Get current time in given timezone. Extracted for testability."""
        return datetime.now(tz)

    def check_ntp_offset(self) -> float | None:
        """Check system clock offset against NTP. Returns seconds offset or None.

        BLOCKING — use check_ntp_offset_async() in async contexts.
        """
        try:
            import ntplib

            client = ntplib.NTPClient()
            resp = client.request("pool.ntp.org", version=3)
            self._ntp_offset = resp.offset
            return resp.offset
        except Exception:
            return None

    async def check_ntp_offset_async(self) -> float | None:
        """Non-blocking NTP check — runs in a thread pool to avoid blocking the event loop.

        Saves 2-5s vs blocking check_ntp_offset().
        """
        try:
            offset = await asyncio.to_thread(self.check_ntp_offset)
            return offset
        except Exception:
            return None

    def set_resy_offset(self, offset: float) -> None:
        """Set the Resy server clock offset (from ResyApiClient.calibrate_resy_clock).

        Positive = Resy clock is ahead of ours (we should fire earlier).
        """
        self._resy_offset = offset
        logger.info("Resy clock offset set: %.1fms", offset * 1000)

    def compensate_drop_time(self, drop_time: datetime) -> datetime:
        """Apply all clock offsets to the drop time.

        Combines NTP offset + Resy server clock offset for maximum accuracy.
        """
        total = self.total_offset
        if total != 0.0:
            logger.info("Total clock compensation: %.1fms", total * 1000)
            return drop_time - timedelta(seconds=total)
        return drop_time
