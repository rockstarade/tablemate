"""Pydantic models for OpenTable API interactions.

These models map to the OpenTable mobile API (mobile-api.opentable.com)
response structures. The ``OTSlot.to_common_slot()`` converter bridges
into the shared ``Slot`` model so ``SlotSelector`` works unchanged.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Auth / credentials
# ---------------------------------------------------------------------------


class OTAuthToken(BaseModel):
    """Bearer token + identity extracted from the OpenTable mobile app.

    The user intercepts this via a network proxy (Charles / mitmproxy)
    and pastes the bearer token into TablePass.
    """

    bearer_token: str
    diner_id: str = ""
    gpid: str = ""
    phone: str = ""  # E.164, e.g. "+12125551234"
    first_name: str = ""
    last_name: str = ""
    # Default to NYC coordinates
    latitude: float = Field(default=40.7128)
    longitude: float = Field(default=-74.0060)


# ---------------------------------------------------------------------------
# Availability / slots
# ---------------------------------------------------------------------------


class OTSlot(BaseModel):
    """Single availability slot from PUT /api/v3/restaurant/availability.

    ``slot_hash`` is the opaque identifier passed to the lock endpoint,
    analogous to Resy's ``config.token`` / ``book_token``.
    """

    slot_hash: str
    date_time: str  # ISO 8601, e.g. "2026-03-26T19:00"
    slot_type: str = ""  # "Standard", "Bar", "Patio", etc.
    available: bool = True

    def to_common_slot(self):
        """Convert to the shared Slot model so SlotSelector works.

        Maps:
          slot_hash  → config.token  (booking identifier)
          slot_type  → config.type   (seating type filter)
          date_time  → date.start    (datetime string)
        """
        from tablement.models import Slot, SlotConfig, SlotDate

        # SlotSelector expects "YYYY-MM-DD HH:MM:SS" in date.start
        start_str = self.date_time.replace("T", " ")
        if len(start_str) == 16:  # "2026-03-26 19:00" → add ":00"
            start_str += ":00"

        return Slot(
            config=SlotConfig(
                id=self.slot_hash,
                type=self.slot_type,
                token=self.slot_hash,
            ),
            date=SlotDate(start=start_str),
        )


class OTAvailabilityResponse(BaseModel):
    """Parsed response from PUT /api/v3/restaurant/availability."""

    slots: list[OTSlot] = []
    date_messages: list[str] = []


# ---------------------------------------------------------------------------
# Lock / booking
# ---------------------------------------------------------------------------


class OTLockResponse(BaseModel):
    """Response from POST /api/v1/reservation/{restaurant_id}/lock.

    The ``lock_id`` is passed to the final booking step.
    """

    lock_id: str
    expires_at: str = ""


class OTBookResponse(BaseModel):
    """Response from POST /api/v1/reservation/{restaurant_id}.

    Successful booking returns a confirmation number.
    """

    confirmation_number: str = ""
    reservation_id: str = ""
    restaurant_name: str = ""
    date_time: str = ""
    party_size: int = 0
