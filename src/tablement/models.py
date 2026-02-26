"""Pydantic models for API interactions and domain objects."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr


# --- Credential / Config Models ---


class ResyCredentials(BaseModel):
    """User credentials. Stored in keyring, never serialized to disk."""

    email: str
    password: SecretStr


class TimePreference(BaseModel):
    """Single time+type preference in priority order."""

    time: time
    seating_type: str | None = None


class DropTime(BaseModel):
    """When the restaurant releases reservations."""

    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    second: int = Field(default=0, ge=0, le=59)
    timezone: str = "America/New_York"
    days_ahead: int = 30


class RetryConfig(BaseModel):
    """Controls retry behavior during the snipe window."""

    duration_seconds: float = 10.0
    interval_seconds: float = 0.0  # 0 = tight loop (fastest)
    max_attempts: int = 200


class SnipeConfig(BaseModel):
    """Loaded from YAML config file."""

    venue_id: int
    venue_name: str | None = None
    party_size: int = Field(ge=1, le=20)
    date: date
    time_preferences: list[TimePreference]
    drop_time: DropTime
    retry: RetryConfig = RetryConfig()
    window_minutes: int = 30  # ±N minutes around preferred time


# --- API Response Models ---


class PaymentMethod(BaseModel):
    id: int
    is_default: bool = False
    display: str = ""


class AuthResponse(BaseModel):
    id: int
    token: str
    first_name: str = ""
    last_name: str = ""
    payment_methods: list[PaymentMethod] = []


class SlotConfig(BaseModel):
    id: int | str = ""
    type: str = ""
    token: str = ""


class SlotDate(BaseModel):
    start: str  # "2026-03-26 19:00:00"
    end: str = ""


class Slot(BaseModel):
    config: SlotConfig
    date: SlotDate

    @property
    def start_dt(self) -> datetime:
        return datetime.fromisoformat(self.date.start)


class FindVenue(BaseModel):
    slots: list[Slot] = []


class FindResults(BaseModel):
    venues: list[FindVenue] = []


class FindResponse(BaseModel):
    results: FindResults


class BookToken(BaseModel):
    value: str
    date_expires: str = ""


class DetailsResponse(BaseModel):
    book_token: BookToken


class BookResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    resy_token: str = ""


# --- Snipe Result ---


class SnipeResult(BaseModel):
    success: bool
    resy_token: str | None = None
    slot: Slot | None = None
    attempts: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None

    # Drop intelligence fields (internal, populated by snipe loop — not serialized)
    _drop_detected_at: Any = PrivateAttr(default=None)
    _slots_at_drop: int | None = PrivateAttr(default=None)
    _slot_types_at_drop: list | None = PrivateAttr(default=None)
    _first_slot_time: str | None = PrivateAttr(default=None)
    _last_slot_time: str | None = PrivateAttr(default=None)
    _drop_poll_attempt: int | None = PrivateAttr(default=None)
    _avg_poll_interval_ms: float | None = PrivateAttr(default=None)

    model_config = {"arbitrary_types_allowed": True}
