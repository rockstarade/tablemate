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
    referral_code: str | None = None


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
    opentable_linked: bool = False
    opentable_diner_id: str | None = None
    stripe_linked: bool = False
    referral_code: str | None = None
    gifts_remaining: int = 0
    referral_discount: bool = False
    plan: str = "free"
    plan_bookings_used: int = 0
    plan_period_end: str | None = None


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
    image_url: str = ""


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
    platform: str = "resy"  # "resy" or "opentable"


class BatchReservationRequest(BaseModel):
    """Multi-date reservation request for the smart calendar.

    Creates one reservation per date, all sharing a group_id.
    Released dates → monitor mode (cancellation sniping).
    Unreleased dates → snipe mode (release sniping at drop time).
    """
    venue_id: int
    venue_name: str
    party_size: int = Field(ge=1, le=20)
    dates: list[str] = Field(..., min_length=1, max_length=10)  # YYYY-MM-DD
    time_preferences: list[TimePreferenceIn]
    drop_time: DropTimeIn | None = None   # drop policy for unreleased dates
    book_earliest: bool = False           # "earliest available" toggle
    latest_notify_hours: float = Field(default=2.0, ge=0.5, le=48.0)
    notification_email: str | None = None  # email for booking confirmation
    platform: str = "resy"  # "resy" or "opentable"


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
    attempts: int = 0
    elapsed_seconds: float | None = None
    error: str | None = None
    created_at: str | None = None
    group_id: str | None = None
    poll_tier: str | None = None
    latest_notify_hours: float | None = None


class ReservationListResponse(BaseModel):
    reservations: list[ReservationOut]


class BatchReservationResponse(BaseModel):
    group_id: str
    reservation_ids: list[str]
    count: int


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


class CreditBalanceResponse(BaseModel):
    credits: int


class BuyCreditsRequest(BaseModel):
    package: str  # "single" ($12, 1 credit) or "five_pack" ($50, 5 credits)


class TransactionOut(BaseModel):
    id: str
    type: str
    amount_cents: int
    credits_delta: int = 0
    description: str | None = None
    reservation_id: str | None = None
    created_at: str


class TransactionListResponse(BaseModel):
    transactions: list[TransactionOut]


class SubscribeRequest(BaseModel):
    plan: str  # "pro" or "vip"


class SubscriptionResponse(BaseModel):
    plan: str
    status: str
    stripe_subscription_id: str | None = None
    current_period_start: str | None = None
    current_period_end: str | None = None
    bookings_used: int = 0
    bookings_included: int = 0


class PlanInfoResponse(BaseModel):
    plan: str
    bookings_used: int = 0
    bookings_included: int = 0
    credits: int = 0
    stripe_subscription_id: str | None = None
    period_end: str | None = None


# ---------------------------------------------------------------------------
# Snipe — SSE status (kept for backward compat + real-time updates)
# ---------------------------------------------------------------------------


class SnipeResultOut(BaseModel):
    success: bool
    booking_path: str | None = None
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


# ---------------------------------------------------------------------------
# OpenTable Auth
# ---------------------------------------------------------------------------


class LinkOpenTableRequest(BaseModel):
    """Link an OpenTable account by pasting a Bearer token from the mobile app."""
    bearer_token: str
    diner_id: str = ""  # Auto-extracted from token validation
    gpid: str = ""
    phone: str = ""


class LinkOpenTableResponse(BaseModel):
    linked: bool
    diner_name: str | None = None
    diner_id: str | None = None
