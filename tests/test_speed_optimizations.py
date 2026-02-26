"""Tests for the speed optimization features.

Covers:
- Fast book_token extraction via regex
- Pre-built booking request templates
- Overlapping find_slots (rapid polling)
- Dual-path booking
- Clock calibration (NTP async + Resy server)
- Variable polling frequency for monitor mode
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tablement.api import ResyApiClient, extract_book_token_fast
from tablement.scheduler import PrecisionScheduler
from tablement.web.routes.reservations import _get_poll_interval


# ---------------------------------------------------------------------------
# Phase 1c: Fast book_token extraction
# ---------------------------------------------------------------------------


class TestFastBookTokenExtraction:
    """Test regex-based book_token extraction from /3/details response."""

    def test_extract_from_real_format(self):
        """Extracts book_token from realistic Resy response JSON."""
        content = b'{"book_token": {"value": "rgs://resy/abc-123-def-456", "date_expires": "2026-03-26"}}'
        assert extract_book_token_fast(content) == "rgs://resy/abc-123-def-456"

    def test_extract_with_whitespace(self):
        """Handles whitespace variations in JSON."""
        content = b'{"book_token" : { "value" : "tok_xyz_789" , "date_expires": ""}}'
        assert extract_book_token_fast(content) == "tok_xyz_789"

    def test_extract_from_nested_response(self):
        """Extracts token even when embedded in larger response."""
        content = (
            b'{"venue": {"id": 123}, "book_token": {"value": "rgs://resy/nested-token",'
            b' "date_expires": "2026-04-01"}, "other_field": true}'
        )
        assert extract_book_token_fast(content) == "rgs://resy/nested-token"

    def test_returns_none_on_missing(self):
        """Returns None when book_token is not present."""
        content = b'{"error": "not found"}'
        assert extract_book_token_fast(content) is None

    def test_returns_none_on_empty(self):
        """Returns None for empty content."""
        assert extract_book_token_fast(b"") is None

    def test_returns_none_on_malformed(self):
        """Returns None on malformed but similar-looking content."""
        content = b'{"book_token": "not_an_object"}'
        assert extract_book_token_fast(content) is None


# ---------------------------------------------------------------------------
# Phase 1a: Pre-built booking templates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestBookTemplate:
    """Test pre-built booking request templates."""

    async def test_prepare_book_template_stores_template(self):
        """Template is stored on the client after preparation."""
        async with ResyApiClient() as client:
            template = client.prepare_book_template(67890)
            assert template is not None
            assert "headers" in template
            assert "static_params" in template
            assert client._book_template is template

    async def test_template_contains_payment_method(self):
        """Template includes serialized payment method."""
        async with ResyApiClient() as client:
            template = client.prepare_book_template(12345)
            assert '"id": 12345' in template["static_params"]["struct_payment_method"]

    async def test_template_contains_source_id(self):
        """Template includes source_id."""
        async with ResyApiClient() as client:
            template = client.prepare_book_template(99)
            assert template["static_params"]["source_id"] == "resy.com-venue-details"

    async def test_template_headers_use_widget_origin(self):
        """Template headers use widgets.resy.com origin."""
        async with ResyApiClient() as client:
            template = client.prepare_book_template(1)
            assert template["headers"]["Origin"] == "https://widgets.resy.com"


# ---------------------------------------------------------------------------
# Phase 1b: Overlapping find_slots (rapid polling)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestFindSlotsRapid:
    """Test overlapping find_slots polling."""

    async def test_returns_first_nonempty_result(self):
        """Returns slots as soon as one volley finds them."""
        from tablement.models import Slot, SlotConfig, SlotDate

        slot = Slot(
            config=SlotConfig(id="1", type="Dining Room", token="tok"),
            date=SlotDate(start="2026-03-26 19:00:00"),
        )

        async with ResyApiClient() as client:
            # Mock find_slots to return empty first, then slot
            call_count = 0

            async def _mock_find(*args, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count >= 2:
                    return [slot]
                return []

            client.find_slots = _mock_find
            result = await client.find_slots_rapid(123, "2026-03-26", 2, volleys=3, stagger_ms=10)

        assert len(result) == 1
        assert result[0].config.token == "tok"

    async def test_returns_empty_when_all_empty(self):
        """Returns empty list when all volleys find nothing."""
        async with ResyApiClient() as client:
            client.find_slots = AsyncMock(return_value=[])
            result = await client.find_slots_rapid(123, "2026-03-26", 2, volleys=3, stagger_ms=10)
        assert result == []

    async def test_fires_multiple_volleys(self):
        """Fires the specified number of concurrent requests."""
        async with ResyApiClient() as client:
            client.find_slots = AsyncMock(return_value=[])
            await client.find_slots_rapid(123, "2026-03-26", 2, volleys=4, stagger_ms=10)
        assert client.find_slots.call_count == 4


# ---------------------------------------------------------------------------
# Phase 2: Dual-path booking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDualPathBooking:
    """Test simultaneous direct + proxy booking."""

    async def test_falls_back_to_single_path_without_direct(self):
        """Falls back to regular book() when no direct client is warmed."""
        from tablement.models import BookResponse

        async with ResyApiClient() as client:
            client.book = AsyncMock(
                return_value=BookResponse(resy_token="single_path_tok")
            )
            result = await client.dual_book("book_tok", 123)
            assert result.resy_token == "single_path_tok"
            client.book.assert_called_once()

    async def test_direct_client_created_by_warmup(self):
        """warmup_direct creates a direct client."""
        async with ResyApiClient() as client:
            assert client._direct_client is None
            await client.warmup_direct()
            assert client._direct_client is not None
            # Cleanup
            await client._direct_client.aclose()
            client._direct_client = None


# ---------------------------------------------------------------------------
# Phase 3: Clock calibration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestClockCalibration:
    """Test Resy server clock calibration."""

    async def test_calibrate_returns_float(self):
        """Calibration returns a float offset."""
        from email.utils import formatdate

        async with ResyApiClient() as client:
            # Mock the HEAD request to return a Date header
            mock_resp = MagicMock()
            mock_resp.headers = {"date": formatdate(usegmt=True)}
            client._client.head = AsyncMock(return_value=mock_resp)

            offset = await client.calibrate_resy_clock(samples=2)
            assert isinstance(offset, float)
            # Offset should be small (both clocks are local)
            assert abs(offset) < 2.0

    async def test_calibrate_handles_failure(self):
        """Returns 0.0 when calibration fails."""
        async with ResyApiClient() as client:
            client._client.head = AsyncMock(side_effect=Exception("connection failed"))
            offset = await client.calibrate_resy_clock(samples=2)
            assert offset == 0.0


class TestSchedulerNonBlockingNTP:
    """Test non-blocking NTP check."""

    @pytest.mark.asyncio
    async def test_async_ntp_returns_none_without_ntplib(self):
        """Returns None when ntplib not available or NTP unreachable."""
        scheduler = PrecisionScheduler()
        # Patch the sync method to return None (simulates ntplib failure)
        scheduler.check_ntp_offset = MagicMock(return_value=None)
        result = await scheduler.check_ntp_offset_async()
        assert result is None

    def test_compensate_drop_time_no_offset(self):
        """No compensation when offsets are zero."""
        scheduler = PrecisionScheduler()
        dt = datetime(2026, 3, 26, 10, 0, 0)
        assert scheduler.compensate_drop_time(dt) == dt

    def test_compensate_drop_time_with_ntp(self):
        """NTP offset shifts drop time."""
        scheduler = PrecisionScheduler()
        scheduler._ntp_offset = 0.1  # 100ms ahead
        dt = datetime(2026, 3, 26, 10, 0, 0)
        result = scheduler.compensate_drop_time(dt)
        # Should be 100ms earlier
        assert result < dt

    def test_compensate_drop_time_with_resy_offset(self):
        """Resy clock offset shifts drop time."""
        scheduler = PrecisionScheduler()
        scheduler.set_resy_offset(0.05)  # 50ms ahead
        dt = datetime(2026, 3, 26, 10, 0, 0)
        result = scheduler.compensate_drop_time(dt)
        assert result < dt

    def test_total_offset_combines_both(self):
        """Total offset is NTP + Resy."""
        scheduler = PrecisionScheduler()
        scheduler._ntp_offset = 0.1
        scheduler.set_resy_offset(0.05)
        assert abs(scheduler.total_offset - 0.15) < 1e-9


# ---------------------------------------------------------------------------
# Phase 5b: Variable polling frequency
# ---------------------------------------------------------------------------


class TestVariablePollingFrequency:
    """Test cancellation-probability-based polling intervals."""

    def test_normal_interval(self):
        """Returns base 30s interval for normal times."""
        now = datetime(2026, 3, 20, 14, 0)  # 6 days before, 2pm
        interval = _get_poll_interval("2026-03-26", now)
        assert interval == 30

    def test_peak_24_48h_window(self):
        """Polls at 15s during 24-48h before reservation."""
        now = datetime(2026, 3, 24, 10, 0)  # 36h before
        interval = _get_poll_interval("2026-03-26", now)
        assert interval == 15  # 30 * 0.5

    def test_peak_day_of_morning(self):
        """Polls at 15s during 2-6h before reservation."""
        now = datetime(2026, 3, 25, 20, 0)  # 4h before midnight
        interval = _get_poll_interval("2026-03-26", now)
        assert interval == 15  # 30 * 0.5

    def test_lunch_decision_time(self):
        """Polls at 21s during lunch decision hours."""
        now = datetime(2026, 3, 22, 12, 0)  # 4 days before, noon
        interval = _get_poll_interval("2026-03-26", now)
        assert interval == 21  # 30 * 0.7

    def test_dinner_decision_time(self):
        """Polls at 21s during dinner decision hours."""
        now = datetime(2026, 3, 22, 17, 30)  # 4 days before, 5:30pm
        interval = _get_poll_interval("2026-03-26", now)
        assert interval == 21  # 30 * 0.7

    def test_invalid_date_returns_base(self):
        """Returns base interval for invalid date."""
        interval = _get_poll_interval("not-a-date")
        assert interval == 30
