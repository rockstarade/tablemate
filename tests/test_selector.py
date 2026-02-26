"""Tests for the slot selection algorithm."""

from datetime import date, time

import pytest

from tablement.models import FindResponse, Slot, TimePreference
from tablement.selector import SlotSelector


def _make_slots(find_response_dict: dict) -> list[Slot]:
    """Parse slots from a find response dict."""
    resp = FindResponse.model_validate(find_response_dict)
    slots = []
    for venue in resp.results.venues:
        slots.extend(venue.slots)
    return slots


class TestSlotSelector:
    def setup_method(self):
        self.selector = SlotSelector(window_minutes=30)
        self.target_date = date(2026, 3, 26)

    def test_exact_time_and_type_match(self, sample_find_response):
        slots = _make_slots(sample_find_response)
        prefs = [TimePreference(time=time(19, 0), seating_type="Dining Room")]

        result = self.selector.select(slots, prefs, self.target_date)

        assert result is not None
        assert result.config.id == "slot-2"
        assert result.config.type == "Dining Room"

    def test_exact_time_any_type(self, sample_find_response):
        slots = _make_slots(sample_find_response)
        prefs = [TimePreference(time=time(19, 0))]  # No seating_type filter

        result = self.selector.select(slots, prefs, self.target_date)

        assert result is not None
        # Should pick 19:00 (exact match), either Dining Room or Bar
        assert "19:00" in result.date.start

    def test_priority_ordering(self, sample_find_response):
        """First preference takes priority even if later ones are closer."""
        slots = _make_slots(sample_find_response)
        prefs = [
            TimePreference(time=time(19, 0), seating_type="Bar"),
            TimePreference(time=time(19, 0), seating_type="Dining Room"),
        ]

        result = self.selector.select(slots, prefs, self.target_date)

        assert result is not None
        assert result.config.type == "Bar"

    def test_falls_to_second_preference(self, sample_find_response):
        """If first preference has no match, falls to second."""
        slots = _make_slots(sample_find_response)
        prefs = [
            TimePreference(time=time(19, 0), seating_type="Chef's Table"),  # No match
            TimePreference(time=time(19, 0), seating_type="Patio"),
        ]

        result = self.selector.select(slots, prefs, self.target_date)

        assert result is not None
        assert result.config.type == "Patio"

    def test_window_matching(self, sample_find_response):
        """Matches within the time window when exact time isn't available."""
        slots = _make_slots(sample_find_response)
        prefs = [TimePreference(time=time(19, 10), seating_type="Patio")]

        result = self.selector.select(slots, prefs, self.target_date)

        assert result is not None
        assert result.config.type == "Patio"
        assert result.config.id == "slot-4"  # 19:15 Patio, closest to 19:10

    def test_picks_closest_in_window(self, sample_find_response):
        """When multiple slots are in the window, picks the closest."""
        slots = _make_slots(sample_find_response)
        # 18:45 is between 18:30 and 19:00 — 18:30 is closer (15 min vs 15 min, but 18:30 comes first)
        prefs = [TimePreference(time=time(18, 40), seating_type="Dining Room")]

        result = self.selector.select(slots, prefs, self.target_date)

        assert result is not None
        assert result.config.id == "slot-1"  # 18:30, 10 min away (vs 19:00 which is 20 min)

    def test_outside_window_no_match(self, sample_find_response):
        """Slots outside the window are not matched."""
        slots = _make_slots(sample_find_response)
        prefs = [TimePreference(time=time(22, 0))]  # Way past any slot

        result = self.selector.select(slots, prefs, self.target_date)

        assert result is None

    def test_empty_slots(self):
        prefs = [TimePreference(time=time(19, 0))]
        result = self.selector.select([], prefs, self.target_date)
        assert result is None

    def test_empty_preferences(self, sample_find_response):
        slots = _make_slots(sample_find_response)
        result = self.selector.select(slots, [], self.target_date)
        assert result is None

    def test_case_insensitive_seating_type(self, sample_find_response):
        slots = _make_slots(sample_find_response)
        prefs = [TimePreference(time=time(19, 0), seating_type="dining room")]

        result = self.selector.select(slots, prefs, self.target_date)

        assert result is not None
        assert result.config.type == "Dining Room"
