"""Tests for the snipe orchestrator."""

from datetime import date, datetime, time, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from tablement.models import (
    BookResponse,
    BookToken,
    DetailsResponse,
    DropTime,
    RetryConfig,
    Slot,
    SlotConfig,
    SlotDate,
    SnipeConfig,
    SnipeResult,
    TimePreference,
)
from tablement.sniper import ReservationSniper


def _make_config(drop_in_past: bool = True) -> SnipeConfig:
    """Create a config with drop time in the past for immediate execution."""
    et = ZoneInfo("America/New_York")
    if drop_in_past:
        # Set drop time to 1 minute ago so the sniper runs immediately
        target_date = date.today() + timedelta(days=30)
    else:
        target_date = date(2026, 3, 26)

    return SnipeConfig(
        venue_id=12345,
        venue_name="Test Restaurant",
        party_size=2,
        date=target_date,
        time_preferences=[
            TimePreference(time=time(19, 0), seating_type="Dining Room"),
            TimePreference(time=time(19, 30)),
        ],
        drop_time=DropTime(
            hour=0, minute=0, timezone="America/New_York", days_ahead=30
        ),
        retry=RetryConfig(duration_seconds=2.0, max_attempts=10),
    )


def _make_slot(
    slot_time: str = "2026-03-26 19:00:00",
    seating_type: str = "Dining Room",
    token: str = "config_token_1",
) -> Slot:
    return Slot(
        config=SlotConfig(id="slot-1", type=seating_type, token=token),
        date=SlotDate(start=slot_time),
    )


@pytest.mark.asyncio
class TestReservationSniper:
    async def test_successful_snipe(self):
        """Full happy path: find -> select -> details -> book."""
        config = _make_config(drop_in_past=True)
        slot = _make_slot(
            slot_time=f"{config.date.isoformat()} 19:00:00",
        )

        sniper = ReservationSniper()

        # Mock auth manager
        mock_creds = MagicMock()
        mock_creds.email = "test@example.com"
        mock_creds.password = MagicMock()
        mock_creds.password.get_secret_value.return_value = "pass"
        sniper.auth_manager.load_credentials = MagicMock(return_value=mock_creds)
        sniper.auth_manager.login = AsyncMock(return_value=("token123", 67890))

        # Mock scheduler to not wait
        sniper.scheduler.wait_until = AsyncMock()
        sniper.scheduler.check_ntp_offset_async = AsyncMock(return_value=None)
        sniper.scheduler.compensate_drop_time = MagicMock(
            side_effect=lambda dt: dt
        )

        # Mock API client
        with patch("tablement.sniper.ResyApiClient") as MockClient:
            mock_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            # Optimized snipe loop uses rapid/fast/dual methods
            mock_instance.find_slots_rapid = AsyncMock(return_value=[slot])
            mock_instance.get_details_fast = AsyncMock(return_value="book_tok_123")
            mock_instance.dual_book = AsyncMock(
                return_value=BookResponse(resy_token="confirmed_xyz")
            )
            # Sync methods
            mock_instance.set_auth_token = MagicMock()
            mock_instance.set_snipe_mode = MagicMock()
            mock_instance.prepare_book_template = MagicMock()
            # Async methods
            mock_instance.ping = AsyncMock()
            mock_instance.find_slots = AsyncMock(return_value=[])
            mock_instance.calibrate_resy_clock = AsyncMock(return_value=0.0)
            mock_instance.warmup_direct = AsyncMock()

            result = await sniper.execute(config)

        assert result.success is True
        assert result.resy_token == "confirmed_xyz"
        assert result.attempts >= 1

    async def test_dry_run_skips_booking(self):
        """Dry run finds and selects but does not book."""
        config = _make_config(drop_in_past=True)
        slot = _make_slot(
            slot_time=f"{config.date.isoformat()} 19:00:00",
        )

        sniper = ReservationSniper()
        mock_creds = MagicMock()
        sniper.auth_manager.load_credentials = MagicMock(return_value=mock_creds)
        sniper.auth_manager.login = AsyncMock(return_value=("token123", 67890))
        sniper.scheduler.wait_until = AsyncMock()
        sniper.scheduler.check_ntp_offset_async = AsyncMock(return_value=None)
        sniper.scheduler.compensate_drop_time = MagicMock(
            side_effect=lambda dt: dt
        )

        with patch("tablement.sniper.ResyApiClient") as MockClient:
            mock_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            mock_instance.find_slots_rapid = AsyncMock(return_value=[slot])
            mock_instance.set_auth_token = MagicMock()
            mock_instance.set_snipe_mode = MagicMock()
            mock_instance.prepare_book_template = MagicMock()
            mock_instance.ping = AsyncMock()
            mock_instance.find_slots = AsyncMock(return_value=[])
            mock_instance.calibrate_resy_clock = AsyncMock(return_value=0.0)
            mock_instance.warmup_direct = AsyncMock()

            result = await sniper.execute(config, dry_run=True)

        assert result.success is True
        assert "DRY RUN" in (result.error or "")
        # dual_book and get_details_fast should not have been called
        mock_instance.dual_book.assert_not_called()
        mock_instance.get_details_fast.assert_not_called()

    async def test_retries_on_empty_slots(self):
        """Retries when no slots are available, then succeeds."""
        config = _make_config(drop_in_past=True)
        slot = _make_slot(
            slot_time=f"{config.date.isoformat()} 19:00:00",
        )

        sniper = ReservationSniper()
        mock_creds = MagicMock()
        sniper.auth_manager.load_credentials = MagicMock(return_value=mock_creds)
        sniper.auth_manager.login = AsyncMock(return_value=("token123", 67890))
        sniper.scheduler.wait_until = AsyncMock()
        sniper.scheduler.check_ntp_offset_async = AsyncMock(return_value=None)
        sniper.scheduler.compensate_drop_time = MagicMock(
            side_effect=lambda dt: dt
        )

        with patch("tablement.sniper.ResyApiClient") as MockClient:
            mock_instance = AsyncMock()
            MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            MockClient.return_value.__aexit__ = AsyncMock(return_value=False)

            # find_slots_rapid returns: 3 empty volleys, then slots on 4th
            mock_instance.find_slots_rapid = AsyncMock(
                side_effect=[[], [], [], [slot]]
            )
            mock_instance.get_details_fast = AsyncMock(return_value="book_tok_123")
            mock_instance.dual_book = AsyncMock(
                return_value=BookResponse(resy_token="confirmed_xyz")
            )
            mock_instance.set_auth_token = MagicMock()
            mock_instance.set_snipe_mode = MagicMock()
            mock_instance.prepare_book_template = MagicMock()
            mock_instance.ping = AsyncMock()
            # Warmup calls
            mock_instance.find_slots = AsyncMock(return_value=[])
            mock_instance.calibrate_resy_clock = AsyncMock(return_value=0.0)
            mock_instance.warmup_direct = AsyncMock()

            result = await sniper.execute(config)

        assert result.success is True
        assert result.attempts >= 4  # At least 3 empty + 1 success
