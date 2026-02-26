"""Tests for precision timing scheduler."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from tablement.models import DropTime, RetryConfig, SnipeConfig, TimePreference
from tablement.scheduler import PrecisionScheduler


def _make_config(
    target_date: str = "2026-03-26",
    hour: int = 10,
    minute: int = 0,
    days_ahead: int = 30,
    tz: str = "America/New_York",
) -> SnipeConfig:
    return SnipeConfig(
        venue_id=1,
        party_size=2,
        date=date.fromisoformat(target_date),
        time_preferences=[TimePreference(time=time(19, 0))],
        drop_time=DropTime(
            hour=hour, minute=minute, timezone=tz, days_ahead=days_ahead
        ),
    )


class TestPrecisionScheduler:
    def setup_method(self):
        self.scheduler = PrecisionScheduler()

    def test_calculate_drop_datetime_basic(self):
        config = _make_config("2026-03-26", hour=10, minute=0, days_ahead=30)
        dt = self.scheduler.calculate_drop_datetime(config)

        et = ZoneInfo("America/New_York")
        expected = datetime(2026, 2, 24, 10, 0, 0, tzinfo=et)
        assert dt == expected

    def test_calculate_drop_datetime_14_days(self):
        config = _make_config("2026-03-26", hour=9, minute=30, days_ahead=14)
        dt = self.scheduler.calculate_drop_datetime(config)

        et = ZoneInfo("America/New_York")
        expected = datetime(2026, 3, 12, 9, 30, 0, tzinfo=et)
        assert dt == expected

    def test_calculate_drop_datetime_midnight(self):
        config = _make_config("2026-04-15", hour=0, minute=0, days_ahead=30)
        dt = self.scheduler.calculate_drop_datetime(config)

        et = ZoneInfo("America/New_York")
        expected = datetime(2026, 3, 16, 0, 0, 0, tzinfo=et)
        assert dt == expected

    def test_calculate_drop_datetime_pacific(self):
        config = _make_config(
            "2026-03-26", hour=10, minute=0, days_ahead=30, tz="America/Los_Angeles"
        )
        dt = self.scheduler.calculate_drop_datetime(config)

        pt = ZoneInfo("America/Los_Angeles")
        expected = datetime(2026, 2, 24, 10, 0, 0, tzinfo=pt)
        assert dt == expected

    @pytest.mark.asyncio
    async def test_wait_until_past_returns_immediately(self):
        """If the target is in the past, wait_until should return immediately."""
        et = ZoneInfo("America/New_York")
        past = datetime.now(et) - timedelta(seconds=10)
        # Should not hang
        await self.scheduler.wait_until(past)

    @pytest.mark.asyncio
    async def test_wait_until_very_soon(self):
        """Waiting for a time 0.1s in the future should work."""
        et = ZoneInfo("America/New_York")
        target = datetime.now(et) + timedelta(milliseconds=100)
        await self.scheduler.wait_until(target, pre_wake_seconds=0.5)
        # Should have waited approximately 100ms
        now = datetime.now(et)
        assert now >= target
