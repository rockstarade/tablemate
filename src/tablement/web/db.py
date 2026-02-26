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
    """Get all active claims for a venue+date (for conflict checking)."""
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
