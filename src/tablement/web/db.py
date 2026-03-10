"""Supabase async client init and DB helper functions.

Uses two Supabase clients:
- "auth client" (anon key): for auth operations (OTP, verify, get_user)
- "service client" (service key): for DB operations that bypass RLS

The auth client is what frontend tokens are validated against.
The service client has full DB access for server-side CRUD.
"""

from __future__ import annotations

import logging
import os

from supabase import AsyncClient, acreate_client

logger = logging.getLogger(__name__)

# Two clients: auth (anon key) and service (service role key)
_auth_client: AsyncClient | None = None
_service_client: AsyncClient | None = None


async def init_supabase() -> AsyncClient:
    """Create the async Supabase clients. Call once at startup."""
    global _auth_client, _service_client

    url = os.environ.get("SUPABASE_URL", "")
    anon_key = os.environ.get("SUPABASE_ANON_KEY", "")
    service_key = os.environ.get("SUPABASE_SERVICE_KEY", "")

    if not url or not anon_key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_ANON_KEY environment variables are required"
        )

    # Auth client: uses anon key, respects RLS, used for auth operations
    _auth_client = await acreate_client(url, anon_key)
    logger.info("Supabase auth client initialized (%s)", url)

    # Service client: uses service role key, bypasses RLS, used for DB operations
    if service_key:
        _service_client = await acreate_client(url, service_key)
        logger.info("Supabase service client initialized (full DB access)")
    else:
        logger.warning(
            "SUPABASE_SERVICE_KEY not set — using anon key for DB operations. "
            "Some operations may fail due to RLS policies."
        )
        _service_client = _auth_client

    return _auth_client


def get_client() -> AsyncClient:
    """Get the auth client (for auth operations like OTP, token verification)."""
    if _auth_client is None:
        raise RuntimeError("Supabase client not initialized — call init_supabase() first")
    return _auth_client


def get_service_client() -> AsyncClient:
    """Get the service client (for DB operations that need to bypass RLS)."""
    if _service_client is None:
        raise RuntimeError("Supabase client not initialized — call init_supabase() first")
    return _service_client


# ---------------------------------------------------------------------------
# DB helpers — use service client for server-side operations
# ---------------------------------------------------------------------------


async def get_profile(user_id: str) -> dict | None:
    """Fetch a user profile by user ID."""
    resp = await get_service_client().table("profiles").select("*").eq("id", user_id).execute()
    rows = resp.data
    return rows[0] if rows else None


async def upsert_profile(user_id: str, **fields) -> dict:
    """Create or update a user profile."""
    data = {"id": user_id, **fields}
    resp = await get_service_client().table("profiles").upsert(data).execute()
    return resp.data[0]


async def create_reservation(user_id: str, **fields) -> dict:
    """Insert a new reservation row."""
    data = {"user_id": user_id, **fields}
    resp = await get_service_client().table("reservations").insert(data).execute()
    return resp.data[0]


async def get_reservation(reservation_id: str, user_id: str) -> dict | None:
    """Fetch a single reservation owned by user."""
    resp = (
        await get_service_client()
        .table("reservations")
        .select("*")
        .eq("id", reservation_id)
        .eq("user_id", user_id)
        .execute()
    )
    rows = resp.data
    return rows[0] if rows else None


async def list_reservations(user_id: str) -> list[dict]:
    """List all reservations for a user, newest first."""
    resp = (
        await get_service_client()
        .table("reservations")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data


async def update_reservation(reservation_id: str, **fields) -> dict | None:
    """Update reservation fields. Returns updated row or None."""
    resp = (
        await get_service_client()
        .table("reservations")
        .update(fields)
        .eq("id", reservation_id)
        .execute()
    )
    rows = resp.data
    return rows[0] if rows else None


async def delete_reservation(reservation_id: str) -> None:
    """Hard-delete a reservation row."""
    await get_service_client().table("reservations").delete().eq("id", reservation_id).execute()


# --- Payment methods ---


async def list_payment_methods(user_id: str) -> list[dict]:
    """List saved payment methods for a user."""
    resp = (
        await get_service_client()
        .table("payment_methods")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .execute()
    )
    return resp.data


async def create_payment_method(user_id: str, **fields) -> dict:
    """Save a new payment method."""
    data = {"user_id": user_id, **fields}
    resp = await get_service_client().table("payment_methods").insert(data).execute()
    return resp.data[0]


async def delete_payment_method(pm_id: str, user_id: str) -> None:
    """Delete a payment method owned by user."""
    await (
        get_service_client()
        .table("payment_methods")
        .delete()
        .eq("id", pm_id)
        .eq("user_id", user_id)
        .execute()
    )


# --- Credits + Transactions ---


async def get_user_credits(user_id: str) -> int:
    """Return the user's current credit balance."""
    profile = await get_profile(user_id)
    return (profile or {}).get("credits", 0)


async def add_credits(user_id: str, amount: int) -> int:
    """Add credits to a user's balance atomically. Returns new balance."""
    client = get_service_client()
    # Fetch current, then update — use the fetched value for the write
    profile = await get_profile(user_id)
    current = (profile or {}).get("credits", 0)
    new_balance = current + amount
    await upsert_profile(user_id, credits=new_balance)
    return new_balance


async def deduct_credit(user_id: str) -> bool:
    """Deduct 1 credit atomically. Returns True if successful, False if insufficient.

    Uses a conditional update (credits > 0) to prevent race conditions.
    """
    client = get_service_client()
    # Atomic: only update if credits > 0, then check if a row was affected
    profile = await get_profile(user_id)
    current = (profile or {}).get("credits", 0)
    if current <= 0:
        return False
    # Use a conditional update: SET credits = current - 1 WHERE id = user_id AND credits = current
    # This ensures no race: if another request already decremented, credits != current, no rows updated
    resp = (
        await client.table("profiles")
        .update({"credits": current - 1})
        .eq("id", user_id)
        .eq("credits", current)
        .execute()
    )
    if not resp.data:
        # Race condition: credits changed between read and write, re-read and retry once
        profile = await get_profile(user_id)
        current = (profile or {}).get("credits", 0)
        if current <= 0:
            return False
        resp = (
            await client.table("profiles")
            .update({"credits": current - 1})
            .eq("id", user_id)
            .eq("credits", current)
            .execute()
        )
        return bool(resp.data)
    return True


async def create_transaction(user_id: str, **fields) -> dict:
    """Record a billing transaction."""
    data = {"user_id": user_id, **fields}
    resp = await get_service_client().table("transactions").insert(data).execute()
    return resp.data[0]


async def list_transactions(user_id: str, limit: int = 50) -> list[dict]:
    """List billing transactions for a user, newest first."""
    resp = (
        await get_service_client()
        .table("transactions")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data


async def get_profile_by_phone(phone: str) -> dict | None:
    """Fetch a user profile by phone number."""
    resp = (
        await get_service_client()
        .table("profiles")
        .select("*")
        .eq("phone", phone)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


async def get_profile_by_referral_code(code: str) -> dict | None:
    """Fetch a user profile by referral code."""
    resp = (
        await get_service_client()
        .table("profiles")
        .select("*")
        .eq("referral_code", code)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


async def decrement_gifts(user_id: str) -> bool:
    """Decrement gifts_remaining by 1 atomically. Returns True if successful."""
    client = get_service_client()
    profile = await get_profile(user_id)
    current = (profile or {}).get("gifts_remaining", 0)
    if current <= 0:
        return False
    # Conditional update: only decrement if value hasn't changed (prevents race)
    resp = (
        await client.table("profiles")
        .update({"gifts_remaining": current - 1})
        .eq("id", user_id)
        .eq("gifts_remaining", current)
        .execute()
    )
    return bool(resp.data)


async def get_default_payment_method(user_id: str) -> dict | None:
    """Get the user's default payment method."""
    resp = (
        await get_service_client()
        .table("payment_methods")
        .select("*")
        .eq("user_id", user_id)
        .eq("is_default", True)
        .limit(1)
        .execute()
    )
    rows = resp.data
    if rows:
        return rows[0]
    # Fallback: grab the most recent one
    resp = (
        await get_service_client()
        .table("payment_methods")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    return resp.data[0] if resp.data else None


# ---------------------------------------------------------------------------
# Drop Intelligence — tracked venues, observations, snapshots
# ---------------------------------------------------------------------------


async def list_tracked_venues(active_only: bool = True) -> list[dict]:
    """List venues being tracked for drop intelligence."""
    q = get_service_client().table("tracked_venues").select("*")
    if active_only:
        q = q.eq("active", True)
    resp = await q.order("venue_name").execute()
    return resp.data


async def upsert_tracked_venue(venue_id: int, **fields) -> dict:
    """Create or update a tracked venue."""
    data = {"venue_id": venue_id, **fields}
    resp = await get_service_client().table("tracked_venues").upsert(
        data, on_conflict="venue_id"
    ).execute()
    return resp.data[0]


async def delete_tracked_venue(venue_id: int) -> None:
    """Stop tracking a venue."""
    await (
        get_service_client()
        .table("tracked_venues")
        .update({"active": False})
        .eq("venue_id", venue_id)
        .execute()
    )


async def insert_drop_observation(data: dict) -> dict:
    """Record a drop observation (when slots appeared)."""
    resp = await get_service_client().table("drop_observations").insert(data).execute()
    return resp.data[0]


async def get_drop_observations(
    venue_id: int, *, limit: int = 30
) -> list[dict]:
    """Get recent drop observations for a venue."""
    resp = (
        await get_service_client()
        .table("drop_observations")
        .select("*")
        .eq("venue_id", venue_id)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data


async def get_all_drop_observations(*, limit: int = 100) -> list[dict]:
    """Get recent drop observations across all venues."""
    resp = (
        await get_service_client()
        .table("drop_observations")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data


async def insert_slot_snapshot(data: dict) -> dict:
    """Record a slot snapshot (periodic count of available slots)."""
    resp = await get_service_client().table("slot_snapshots").insert(data).execute()
    return resp.data[0]


async def batch_insert_slot_snapshots(snapshots: list[dict]) -> int:
    """Batch-insert slot snapshots (from inline poll recording).

    Used after a snipe completes to flush all poll data in a single DB call.
    Typically 100-500 rows per snipe (~100KB). Supabase handles this fine.
    Returns the number of rows inserted.
    """
    if not snapshots:
        return 0
    try:
        resp = await get_service_client().table("slot_snapshots").insert(snapshots).execute()
        return len(resp.data) if resp.data else 0
    except Exception as e:
        logger.warning("Batch insert slot snapshots failed (%d rows): %s", len(snapshots), e)
        # Try inserting in smaller chunks if the batch is too large
        if len(snapshots) > 100:
            inserted = 0
            for i in range(0, len(snapshots), 50):
                chunk = snapshots[i : i + 50]
                try:
                    resp = await get_service_client().table("slot_snapshots").insert(chunk).execute()
                    inserted += len(resp.data) if resp.data else 0
                except Exception:
                    continue
            return inserted
        return 0


async def get_slot_snapshots(
    venue_id: int, target_date: str, *, limit: int = 100
) -> list[dict]:
    """Get slot snapshots for a venue+date (velocity tracking)."""
    resp = (
        await get_service_client()
        .table("slot_snapshots")
        .select("*")
        .eq("venue_id", venue_id)
        .eq("target_date", target_date)
        .order("captured_at")
        .limit(limit)
        .execute()
    )
    return resp.data


# ---------------------------------------------------------------------------
# Slot Claims — multi-user conflict tracking
# ---------------------------------------------------------------------------


async def create_slot_claim(user_id: str, **fields) -> dict:
    """Create a slot claim for a user."""
    data = {"user_id": user_id, **fields}
    resp = await get_service_client().table("slot_claims").insert(data).execute()
    return resp.data[0]


async def get_active_claims(venue_id: int, target_date: str) -> list[dict]:
    """Get all active claims for a venue+date (for conflict checking).

    Claims are considered orphaned (and excluded) only when the target_date
    has already passed.  This is safe for both drop snipes (which run briefly)
    and cancellation monitors (which can run for days/weeks until the
    reservation date).
    """
    from datetime import date

    # If the target_date is already in the past, no claims can be relevant
    try:
        if date.fromisoformat(target_date) < date.today():
            return []
    except ValueError:
        pass  # malformed date — fall through to normal query

    resp = (
        await get_service_client()
        .table("slot_claims")
        .select("*")
        .eq("venue_id", venue_id)
        .eq("target_date", target_date)
        .eq("status", "active")
        .execute()
    )
    return resp.data


async def update_slot_claim(claim_id: str, **fields) -> dict | None:
    """Update a slot claim."""
    resp = (
        await get_service_client()
        .table("slot_claims")
        .update(fields)
        .eq("id", claim_id)
        .execute()
    )
    return resp.data[0] if resp.data else None


async def update_claims_by_reservation(reservation_id: str, status: str) -> int:
    """Update all slot_claims linked to a reservation.

    Returns count of updated claims.
    """
    resp = (
        await get_service_client()
        .table("slot_claims")
        .update({"status": status})
        .eq("reservation_id", reservation_id)
        .execute()
    )
    return len(resp.data)


async def get_claims_count(venue_id: int, target_date: str) -> dict[str, int]:
    """Get count of active claims per time slot (anonymous — no user IDs)."""
    claims = await get_active_claims(venue_id, target_date)
    counts: dict[str, int] = {}
    for c in claims:
        t = c.get("preferred_time", "")
        if t:
            counts[t] = counts.get(t, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Curated Restaurants — browse grid
# ---------------------------------------------------------------------------


async def list_curated_restaurants(active_only: bool = True) -> list[dict]:
    """List curated restaurants for the browse grid, ordered by sort_order."""
    q = get_service_client().table("curated_restaurants").select("*")
    if active_only:
        q = q.eq("is_active", True)
    resp = await q.order("sort_order").execute()
    return resp.data


async def get_curated_restaurant(venue_id: int) -> dict | None:
    """Get a single curated restaurant by venue_id."""
    resp = (
        await get_service_client()
        .table("curated_restaurants")
        .select("*")
        .eq("venue_id", venue_id)
        .execute()
    )
    rows = resp.data
    return rows[0] if rows else None


async def update_restaurant_image(venue_id: int, image_url: str | None) -> dict | None:
    """Update the image_url for a curated restaurant."""
    resp = (
        await get_service_client()
        .table("curated_restaurants")
        .update({"image_url": image_url})
        .eq("venue_id", venue_id)
        .execute()
    )
    return resp.data[0] if resp.data else None


async def update_curated_restaurant(venue_id: int, fields: dict) -> dict | None:
    """Update any fields on a curated restaurant.

    Gracefully handles missing columns by retrying without the offending
    field when Supabase returns PGRST204 (column not found).
    """
    allowed = {
        "name", "cuisine", "neighborhood", "url_slug", "image_url",
        "slot_interval", "drop_days_ahead", "drop_hour", "drop_minute",
        "service_start", "service_end", "hot_start", "hot_end",
        "sort_order", "is_active", "price_level",
    }
    data = {k: v for k, v in fields.items() if k in allowed}
    if not data:
        return None

    from postgrest.exceptions import APIError
    import re

    # Retry loop: strip columns that don't exist in the DB yet
    for _ in range(len(data) + 1):
        try:
            resp = (
                await get_service_client()
                .table("curated_restaurants")
                .update(data)
                .eq("venue_id", venue_id)
                .execute()
            )
            return resp.data[0] if resp.data else None
        except APIError as e:
            # Check for PGRST204 (column not found) via all possible
            # representations — APIError.code, repr(), str(), and args.
            err_code = getattr(e, "code", "") or ""
            err_repr = repr(e)
            err_str = str(e)
            err_args = str(e.args) if e.args else ""
            combined = f"{err_code} {err_repr} {err_str} {err_args}"

            if "PGRST204" in combined or "Could not find" in combined:
                # Extract the missing column name from any part of the error
                m = re.search(r"'(\w+)'\s*column", combined) or \
                    re.search(r"column\s*'(\w+)'", combined) or \
                    re.search(r"'(\w+)'", err_str)
                if m and m.group(1) in data:
                    bad_col = m.group(1)
                    data.pop(bad_col, None)
                    if not data:
                        return None
                    continue
            raise
    return None


async def create_curated_restaurant(data: dict) -> dict:
    """Insert a new curated restaurant."""
    resp = (
        await get_service_client()
        .table("curated_restaurants")
        .insert(data)
        .execute()
    )
    return resp.data[0] if resp.data else data


# ---------------------------------------------------------------------------
# Reservation Groups — multi-date cancellation sniping
# ---------------------------------------------------------------------------


async def get_reservation_by_id(reservation_id: str) -> dict | None:
    """Fetch a single reservation by ID (no user check — for admin/internal use)."""
    resp = (
        await get_service_client()
        .table("reservations")
        .select("*")
        .eq("id", reservation_id)
        .execute()
    )
    rows = resp.data
    return rows[0] if rows else None


async def get_reservations_by_group(group_id: str) -> list[dict]:
    """Get all reservations sharing a group_id."""
    resp = (
        await get_service_client()
        .table("reservations")
        .select("*")
        .eq("group_id", group_id)
        .execute()
    )
    return resp.data


async def get_active_group_reservations(group_id: str) -> list[dict]:
    """Get only active (non-terminal) reservations in a group."""
    resp = (
        await get_service_client()
        .table("reservations")
        .select("*")
        .eq("group_id", group_id)
        .in_("status", ["scheduled", "monitoring", "sniping"])
        .execute()
    )
    return resp.data


# ---------------------------------------------------------------------------
# Scout System — campaigns, events, snapshots
# ---------------------------------------------------------------------------


async def list_scout_campaigns(active_only: bool = True) -> list[dict]:
    """List scout campaigns, optionally filtered to active only."""
    q = get_service_client().table("scout_campaigns").select("*")
    if active_only:
        q = q.eq("active", True)
    resp = await q.order("priority", desc=True).execute()
    return resp.data


async def get_scout_campaign(venue_id: int, campaign_type: str | None = None) -> dict | None:
    """Get a single scout campaign by venue_id (and optional type)."""
    q = (
        get_service_client()
        .table("scout_campaigns")
        .select("*")
        .eq("venue_id", venue_id)
    )
    if campaign_type:
        q = q.eq("type", campaign_type)
    resp = await q.execute()
    return resp.data[0] if resp.data else None


async def upsert_scout_campaign(venue_id: int, campaign_type: str = "drop", **fields) -> dict:
    """Create or update a scout campaign."""
    data = {"venue_id": venue_id, "type": campaign_type, **fields}
    resp = await get_service_client().table("scout_campaigns").upsert(
        data, on_conflict="venue_id,type"
    ).execute()
    return resp.data[0]


async def delete_scout_campaign(venue_id: int, campaign_type: str | None = None) -> None:
    """Deactivate a scout campaign."""
    q = (
        get_service_client()
        .table("scout_campaigns")
        .update({"active": False})
        .eq("venue_id", venue_id)
    )
    if campaign_type:
        q = q.eq("type", campaign_type)
    await q.execute()


async def update_scout_campaign_stats(venue_id: int, **fields) -> None:
    """Update stats on a scout campaign (total_polls, last_poll_at, etc.)."""
    await (
        get_service_client()
        .table("scout_campaigns")
        .update(fields)
        .eq("venue_id", venue_id)
        .execute()
    )


async def insert_scout_event(data: dict) -> dict:
    """Record a scout event (availability change)."""
    resp = await get_service_client().table("scout_events").insert(data).execute()
    return resp.data[0]


async def batch_insert_scout_events(events: list[dict]) -> int:
    """Batch-insert scout events. Returns count inserted."""
    if not events:
        return 0
    try:
        resp = await get_service_client().table("scout_events").insert(events).execute()
        return len(resp.data) if resp.data else 0
    except Exception as e:
        logger.warning("Batch insert scout events failed (%d rows): %s", len(events), e)
        return 0


async def get_scout_events(
    venue_id: int | None = None,
    event_type: str | None = None,
    *,
    limit: int = 50,
) -> list[dict]:
    """Get recent scout events, optionally filtered by venue or type."""
    q = get_service_client().table("scout_events").select("*")
    if venue_id is not None:
        q = q.eq("venue_id", venue_id)
    if event_type:
        q = q.eq("event_type", event_type)
    resp = await q.order("captured_at", desc=True).limit(limit).execute()
    return resp.data


async def insert_scout_snapshot(data: dict) -> dict:
    """Record a periodic availability snapshot."""
    resp = await get_service_client().table("scout_snapshots").insert(data).execute()
    return resp.data[0]


async def get_scout_snapshots(
    venue_id: int,
    target_date: str | None = None,
    *,
    limit: int = 100,
) -> list[dict]:
    """Get availability snapshots for a venue."""
    q = (
        get_service_client()
        .table("scout_snapshots")
        .select("*")
        .eq("venue_id", venue_id)
    )
    if target_date:
        q = q.eq("target_date", target_date)
    resp = await q.order("captured_at", desc=True).limit(limit).execute()
    return resp.data


async def get_scout_heatmap_data(venue_id: int | None = None) -> list[dict]:
    """Get aggregated heatmap data: count of slots_appeared events by hour×day.

    Returns rows with {hour_et, day_of_week, event_count}.
    Uses raw Supabase RPC or falls back to client-side aggregation.
    """
    q = (
        get_service_client()
        .table("scout_events")
        .select("hour_et, day_of_week")
        .eq("event_type", "slots_appeared")
    )
    if venue_id is not None:
        q = q.eq("venue_id", venue_id)
    resp = await q.limit(5000).execute()

    # Client-side aggregation into hour×day grid
    grid: dict[tuple[int, int], int] = {}
    for row in resp.data:
        h = row.get("hour_et")
        d = row.get("day_of_week")
        if h is not None and d is not None:
            grid[(h, d)] = grid.get((h, d), 0) + 1

    return [
        {"hour_et": h, "day_of_week": d, "event_count": c}
        for (h, d), c in sorted(grid.items())
    ]


async def get_scout_stats() -> dict:
    """Get aggregate scout stats."""
    campaigns = await list_scout_campaigns(active_only=False)
    active = sum(1 for c in campaigns if c.get("active"))
    total_polls = sum(c.get("total_polls", 0) for c in campaigns)
    total_events = sum(c.get("total_events", 0) for c in campaigns)

    return {
        "total_campaigns": len(campaigns),
        "active_campaigns": active,
        "total_polls": total_polls,
        "total_events": total_events,
    }


async def update_scout_polling_windows(
    venue_id: int, campaign_type: str, polling_windows: list[dict]
) -> dict | None:
    """Update only the polling_windows for a campaign."""
    resp = (
        await get_service_client()
        .table("scout_campaigns")
        .update({"polling_windows": polling_windows})
        .eq("venue_id", venue_id)
        .eq("type", campaign_type)
        .execute()
    )
    return resp.data[0] if resp.data else None


# ---------------------------------------------------------------------------
# Scout Slot Sightings — per-slot lifecycle tracking (Cancellation Scouts)
# ---------------------------------------------------------------------------


async def get_active_sightings(venue_id: int, target_date: str) -> list[dict]:
    """Get currently visible slots (gone_at IS NULL) for a venue+date."""
    resp = (
        await get_service_client()
        .table("scout_slot_sightings")
        .select("*")
        .eq("venue_id", venue_id)
        .eq("target_date", target_date)
        .is_("gone_at", "null")
        .execute()
    )
    return resp.data


async def upsert_sighting(data: dict) -> dict:
    """Insert a new slot sighting."""
    resp = await get_service_client().table("scout_slot_sightings").insert(data).execute()
    return resp.data[0]


async def update_sighting(sighting_id: str, **fields) -> dict | None:
    """Update a sighting (e.g., last_seen_at, poll_count, gone_at)."""
    resp = (
        await get_service_client()
        .table("scout_slot_sightings")
        .update(fields)
        .eq("id", sighting_id)
        .execute()
    )
    return resp.data[0] if resp.data else None


async def close_sighting(sighting_id: str, gone_at: str, duration_seconds: float) -> dict | None:
    """Mark a sighting as disappeared."""
    return await update_sighting(
        sighting_id,
        gone_at=gone_at,
        duration_seconds=duration_seconds,
    )


async def get_slot_timeline(
    venue_id: int,
    target_date: str | None = None,
    *,
    limit: int = 100,
) -> list[dict]:
    """Get slot sighting timeline (recent appear/disappear events)."""
    q = (
        get_service_client()
        .table("scout_slot_sightings")
        .select("*")
        .eq("venue_id", venue_id)
    )
    if target_date:
        q = q.eq("target_date", target_date)
    resp = await q.order("first_seen_at", desc=True).limit(limit).execute()
    return resp.data


async def get_sighting_stats(venue_id: int) -> dict:
    """Get aggregated sighting stats for a venue."""
    resp = (
        await get_service_client()
        .table("scout_slot_sightings")
        .select("*")
        .eq("venue_id", venue_id)
        .not_.is_("gone_at", "null")
        .order("first_seen_at", desc=True)
        .limit(500)
        .execute()
    )
    sightings = resp.data
    if not sightings:
        return {
            "total_sightings": 0,
            "avg_duration_seconds": 0,
            "min_duration_seconds": 0,
            "max_duration_seconds": 0,
            "active_slots": 0,
        }

    durations = [s.get("duration_seconds", 0) for s in sightings if s.get("duration_seconds")]
    active_resp = (
        await get_service_client()
        .table("scout_slot_sightings")
        .select("id", count="exact")
        .eq("venue_id", venue_id)
        .is_("gone_at", "null")
        .execute()
    )

    return {
        "total_sightings": len(sightings),
        "avg_duration_seconds": round(sum(durations) / len(durations), 1) if durations else 0,
        "min_duration_seconds": round(min(durations), 1) if durations else 0,
        "max_duration_seconds": round(max(durations), 1) if durations else 0,
        "active_slots": active_resp.count or 0,
    }


# ---------------------------------------------------------------------------
# Scout Error Log
# ---------------------------------------------------------------------------


async def insert_scout_error(data: dict) -> dict | None:
    """Record a scout error (fire-and-forget safe)."""
    try:
        resp = await get_service_client().table("scout_errors").insert(data).execute()
        return resp.data[0] if resp.data else None
    except Exception:
        return None


async def get_scout_errors(
    venue_id: int | None = None, *, limit: int = 50
) -> list[dict]:
    """Get recent scout errors, optionally filtered by venue."""
    q = get_service_client().table("scout_errors").select("*")
    if venue_id is not None:
        q = q.eq("venue_id", venue_id)
    resp = await q.order("created_at", desc=True).limit(limit).execute()
    return resp.data


async def clear_scout_errors(venue_id: int | None = None) -> None:
    """Clear scout errors. If venue_id given, only clear for that venue."""
    q = get_service_client().table("scout_errors").delete()
    if venue_id:
        q = q.eq("venue_id", venue_id)
    else:
        q = q.neq("id", "00000000-0000-0000-0000-000000000000")
    await q.execute()


# ---------------------------------------------------------------------------
# Scout Settings (single-row table)
# ---------------------------------------------------------------------------


async def get_scout_settings() -> dict | None:
    """Get the single settings row. Returns None if table doesn't exist."""
    try:
        resp = (
            await get_service_client()
            .table("scout_settings")
            .select("*")
            .eq("id", 1)
            .execute()
        )
        return resp.data[0] if resp.data else None
    except Exception:
        return None


async def update_scout_settings(**fields) -> dict:
    """Update scout settings (partial). Creates row if needed."""
    fields.pop("id", None)
    resp = (
        await get_service_client()
        .table("scout_settings")
        .upsert({"id": 1, **fields})
        .execute()
    )
    return resp.data[0] if resp.data else {}


# ---------------------------------------------------------------------------
# Production hardening helpers
# ---------------------------------------------------------------------------


async def has_active_reservation(user_id: str, venue_id: int, target_date: str) -> bool:
    """Check if user already has an active reservation for this venue+date."""
    resp = (
        await get_service_client()
        .table("reservations")
        .select("id")
        .eq("user_id", user_id)
        .eq("venue_id", venue_id)
        .eq("target_date", target_date)
        .not_.in_("status", ["cancelled", "failed", "confirmed"])
        .limit(1)
        .execute()
    )
    return bool(resp.data)


async def has_active_reservation_for_venue(user_id: str, venue_id: int) -> bool:
    """Check if user has ANY active reservation for this venue (any date)."""
    resp = (
        await get_service_client()
        .table("reservations")
        .select("id")
        .eq("user_id", user_id)
        .eq("venue_id", venue_id)
        .not_.in_("status", ["cancelled", "failed", "confirmed", "dry_run"])
        .limit(1)
        .execute()
    )
    return bool(resp.data)


# ---------------------------------------------------------------------------
# Phone ban helpers
# ---------------------------------------------------------------------------

async def is_phone_banned(phone: str) -> bool:
    """Check if a phone number is banned."""
    try:
        resp = (
            await get_service_client()
            .table("banned_phones")
            .select("id")
            .eq("phone", phone)
            .limit(1)
            .execute()
        )
        return bool(resp.data)
    except Exception:
        return False  # table might not exist yet


async def ban_phone(phone: str, reason: str = "") -> dict | None:
    """Ban a phone number."""
    resp = (
        await get_service_client()
        .table("banned_phones")
        .upsert({"phone": phone, "reason": reason}, on_conflict="phone")
        .execute()
    )
    return resp.data[0] if resp.data else None


async def unban_phone(phone: str) -> bool:
    """Remove a phone ban. Returns True if a row was deleted."""
    resp = (
        await get_service_client()
        .table("banned_phones")
        .delete()
        .eq("phone", phone)
        .execute()
    )
    return bool(resp.data)


async def list_banned_phones() -> list[dict]:
    """List all banned phone numbers."""
    try:
        resp = (
            await get_service_client()
            .table("banned_phones")
            .select("*")
            .order("banned_at", desc=True)
            .execute()
        )
        return resp.data or []
    except Exception:
        return []


async def cancel_group_members(group_id: str, except_id: str) -> list[str]:
    """Atomically cancel all group members except the winner. Returns cancelled IDs."""
    resp = (
        await get_service_client()
        .table("reservations")
        .update({"status": "cancelled", "error": "Another reservation in group confirmed"})
        .eq("group_id", group_id)
        .neq("id", except_id)
        .not_.in_("status", ["confirmed", "cancelled", "failed"])
        .execute()
    )
    return [r["id"] for r in resp.data] if resp.data else []


async def count_active_drop_snipes(user_id: str) -> int:
    """Count user's active drop snipes (mode=snipe, status in scheduled/sniping)."""
    resp = (
        await get_service_client()
        .table("reservations")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("mode", "snipe")
        .in_("status", ["scheduled", "sniping"])
        .execute()
    )
    return resp.count or 0


async def count_active_snipes_for_venue(venue_id: int) -> int:
    """Count active snipes targeting a venue (across all users)."""
    resp = (
        await get_service_client()
        .table("reservations")
        .select("id", count="exact")
        .eq("venue_id", venue_id)
        .eq("mode", "snipe")
        .in_("status", ["scheduled", "sniping"])
        .execute()
    )
    return resp.count or 0


async def count_active_monitors(user_id: str) -> int:
    """Count user's active monitors (mode=monitor, status in monitoring/sniping)."""
    resp = (
        await get_service_client()
        .table("reservations")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("mode", "monitor")
        .in_("status", ["monitoring", "sniping"])
        .execute()
    )
    return resp.count or 0


async def get_recoverable_reservations() -> list[dict]:
    """Get all reservations that were in-flight when server crashed."""
    resp = (
        await get_service_client()
        .table("reservations")
        .select("*")
        .in_("status", ["scheduled", "monitoring", "sniping"])
        .execute()
    )
    return resp.data or []


# ---------------------------------------------------------------------------
# User location helpers
# ---------------------------------------------------------------------------

async def update_user_location(
    user_id: str, lat: float, lng: float, city: str | None = None
) -> None:
    """Update user's last known location."""
    from datetime import datetime, timezone

    fields: dict = {
        "last_latitude": lat,
        "last_longitude": lng,
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }
    if city:
        fields["last_location_city"] = city
    await upsert_profile(user_id, **fields)


async def get_user_plan(user_id: str) -> dict:
    """Return the user's subscription plan info.

    Returns dict with keys: plan, stripe_subscription_id, plan_bookings_used,
    plan_period_start, plan_period_end
    """
    profile = await get_profile(user_id)
    if not profile:
        return {"plan": "free", "plan_bookings_used": 0}
    return {
        "plan": profile.get("plan", "free"),
        "stripe_subscription_id": profile.get("stripe_subscription_id"),
        "plan_bookings_used": profile.get("plan_bookings_used", 0),
        "plan_period_start": profile.get("plan_period_start"),
        "plan_period_end": profile.get("plan_period_end"),
    }


async def set_user_plan(
    user_id: str,
    plan: str,
    stripe_subscription_id: str | None = None,
    plan_period_start: str | None = None,
    plan_period_end: str | None = None,
) -> dict:
    """Set the user's subscription plan."""
    fields: dict = {"plan": plan}
    if stripe_subscription_id is not None:
        fields["stripe_subscription_id"] = stripe_subscription_id
    if plan_period_start is not None:
        fields["plan_period_start"] = plan_period_start
    if plan_period_end is not None:
        fields["plan_period_end"] = plan_period_end
    return await upsert_profile(user_id, **fields)


async def increment_plan_bookings(user_id: str) -> int:
    """Increment plan_bookings_used by 1. Returns new count."""
    profile = await get_profile(user_id)
    current = (profile or {}).get("plan_bookings_used", 0)
    new_count = current + 1
    await upsert_profile(user_id, plan_bookings_used=new_count)
    return new_count


async def reset_plan_bookings(user_id: str) -> None:
    """Reset plan_bookings_used to 0 (called on subscription renewal)."""
    await upsert_profile(user_id, plan_bookings_used=0)


async def get_user_locations() -> list[dict]:
    """Get all users that have location data."""
    try:
        resp = (
            await get_service_client()
            .table("profiles")
            .select("id, phone, last_latitude, last_longitude, last_location_city, last_seen_at")
            .not_.is_("last_latitude", "null")
            .execute()
        )
        return resp.data or []
    except Exception:
        return []
