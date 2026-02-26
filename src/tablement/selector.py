"""Slot selection algorithm with priority-ordered preferences."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from tablement.models import Slot, TimePreference


class SlotSelector:
    """
    Selects the best slot from available options based on priority preferences.

    Iterates through the user's priority-ordered TimePreference list.
    For each preference, finds the closest matching slot within a time window.
    First match wins (highest priority preference takes precedence).
    """

    def __init__(self, window_minutes: int = 30) -> None:
        self.window_minutes = window_minutes

    def select(
        self,
        slots: list[Slot],
        preferences: list[TimePreference],
        target_date: date,
    ) -> Slot | None:
        """
        Return the best matching slot, or None if no match found.

        Matching logic per preference (in priority order):
        1. Find slots within the time window
        2. Filter by seating type if specified
        3. Pick the closest to the ideal time
        """
        if not slots or not preferences:
            return None

        for pref in preferences:
            ideal = datetime.combine(target_date, pref.time)
            window = timedelta(minutes=self.window_minutes)

            candidates: list[tuple[float, Slot]] = []
            for slot in slots:
                try:
                    slot_dt = slot.start_dt
                except (ValueError, TypeError):
                    continue

                diff = abs((slot_dt - ideal).total_seconds())
                if diff > window.total_seconds():
                    continue

                # Check seating type match
                if pref.seating_type is not None:
                    if slot.config.type.lower() != pref.seating_type.lower():
                        continue

                candidates.append((diff, slot))

            if candidates:
                # Return the closest slot to the ideal time
                candidates.sort(key=lambda x: x[0])
                return candidates[0][1]

        return None

    def select_top(
        self,
        slots: list[Slot],
        preferences: list[TimePreference],
        target_date: date,
        *,
        limit: int = 3,
    ) -> list[Slot]:
        """Return the top N matching slots for parallel booking.

        Collects all matching candidates across all preferences (priority-ordered),
        deduplicates by config token, and returns up to `limit` unique slots.
        The first slot is always the #1 pick (same as select()). Additional slots
        are fallbacks for parallel get_details().
        """
        if not slots or not preferences:
            return []

        # Collect all candidates across all preferences, tagged with priority
        all_candidates: list[tuple[int, float, Slot]] = []
        seen_tokens: set[str] = set()

        for pref_idx, pref in enumerate(preferences):
            ideal = datetime.combine(target_date, pref.time)
            window = timedelta(minutes=self.window_minutes)

            for slot in slots:
                try:
                    slot_dt = slot.start_dt
                except (ValueError, TypeError):
                    continue

                diff = abs((slot_dt - ideal).total_seconds())
                if diff > window.total_seconds():
                    continue

                if pref.seating_type is not None:
                    if slot.config.type.lower() != pref.seating_type.lower():
                        continue

                token = slot.config.token
                if token not in seen_tokens:
                    seen_tokens.add(token)
                    all_candidates.append((pref_idx, diff, slot))

        if not all_candidates:
            return []

        # Sort by preference priority first, then by time closeness
        all_candidates.sort(key=lambda x: (x[0], x[1]))
        return [slot for _, _, slot in all_candidates[:limit]]
