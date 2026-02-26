"""Pydantic request/response schemas for the web API."""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Auth — Phone OTP
# ---------------------------------------------------------------------------


class OtpSendRequest(BaseModel):
    phone: str = Field(..., description="Phone number in E.164 format, e.g. +14155551234")


class OtpSendResponse(BaseModel):
    sent: bool
    message: str


class OtpVerifyRequest(BaseModel):
    phone: str
    code: str = Field(..., min_length=6, max_length=6)


class OtpVerifyResponse(BaseModel):
    access_token: str
    refresh_token: str
    user_id: str


class LinkResyRequest(BaseModel):
    email: str
    password: str


class LinkResyResponse(BaseModel):
    linked: bool
    resy_first_name: str | None = None
    resy_last_name: str | None = None


class UserProfileResponse(BaseModel):
    user_id: str
    resy_linked: bool = False
    resy_email: str | None = None
    stripe_linked: bool = False


# ---------------------------------------------------------------------------
# Venue Search (unchanged)
# ---------------------------------------------------------------------------


class VenueSearchResult(BaseModel):
    resy_id: int
    name: str
    cuisine: str = ""
    neighborhood: str = ""
    locality: str = ""
    region: str = ""
    url_slug: str = ""
    location_slug: str = ""


class VenueSearchResponse(BaseModel):
    results: list[VenueSearchResult]


# ---------------------------------------------------------------------------
# Venue Lookup (unchanged)
# ---------------------------------------------------------------------------


class VenueLookupRequest(BaseModel):
    url: str


class DetectedPolicyOut(BaseModel):
    days_ahead: int
    hour: int
    minute: int
    timezone: str = "America/New_York"
    source: str = "regex"  # "regex" | "ai"
    confidence: str = "medium"  # "high" | "medium" | "low"
    reasoning: str = ""


class VenueLookupResponse(BaseModel):
    venue_id: int
    venue_name: str | None = None
    detected_policy: DetectedPolicyOut | None = None
    url_date: str | None = None
    url_seats: int | None = None


# ---------------------------------------------------------------------------
# AI Policy (unchanged)
# ---------------------------------------------------------------------------


class PolicyDetectRequest(BaseModel):
    description_text: str
    venue_name: str | None = None


class PolicyDetectResponse(BaseModel):
    detected: bool = False
    days_ahead: int | None = None
    hour: int | None = None
    minute: int | None = None
    timezone: str = "America/New_York"
    confidence: str = "low"
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Reservations — CRUD for booking jobs
# ---------------------------------------------------------------------------


class TimePreferenceIn(BaseModel):
    time: str  # "HH:MM"
    seating_type: str | None = None


class DropTimeIn(BaseModel):
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    second: int = Field(default=0, ge=0, le=59)
    timezone: str = "America/New_York"
    days_ahead: int = 30


class ReservationCreateRequest(BaseModel):
    venue_id: int
    venue_name: str
    party_size: int = Field(ge=1, le=20)
    date: str  # "YYYY-MM-DD"
    mode: str = Field(..., pattern="^(snipe|monitor)$")
    time_preferences: list[TimePreferenceIn]
    drop_time: DropTimeIn | None = None  # required for snipe mode
    dry_run: bool = False


class ReservationOut(BaseModel):
    id: str
    venue_id: int
    venue_name: str
    party_size: int
    target_date: str
    mode: str
    status: str
    time_preferences: list[dict] | None = None
    drop_time_config: dict | None = None
    resy_token: str | None = None
    attempts: int = 0
    elapsed_seconds: float | None = None
    error: str | None = None
    created_at: str | None = None


class ReservationListResponse(BaseModel):
    reservations: list[ReservationOut]


# ---------------------------------------------------------------------------
# Payments — Stripe
# ---------------------------------------------------------------------------


class SetupIntentResponse(BaseModel):
    client_secret: str
    stripe_customer_id: str


class PaymentMethodOut(BaseModel):
    id: str
    brand: str | None = None
    last_four: str | None = None
    is_default: bool = False


class PaymentMethodListResponse(BaseModel):
    methods: list[PaymentMethodOut]


class SavePaymentMethodRequest(BaseModel):
    stripe_payment_method_id: str


# ---------------------------------------------------------------------------
# Snipe — SSE status (kept for backward compat + real-time updates)
# ---------------------------------------------------------------------------


class SnipeResultOut(BaseModel):
    success: bool
    resy_token: str | None = None
    slot_time: str | None = None
    slot_type: str | None = None
    attempts: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None


class SnipeStatusResponse(BaseModel):
    phase: str
    attempt: int = 0
    slots_found: int = 0
    message: str = ""
    result: SnipeResultOut | None = None
    drop_datetime_iso: str | None = None
