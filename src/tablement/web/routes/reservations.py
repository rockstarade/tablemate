"""Reservation CRUD and execution routes.

Handles creating, listing, cancelling reservation jobs (both snipe and monitor).
Each reservation is persisted in Supabase and tracked in-memory via JobState
for real-time SSE updates.

Anti-detection (Layer 2 — Behavioral):
- Monitor mode uses "scout" connections (no auth token) for find_slots() polling.
  The user's Resy token is only used for the 2-3 request booking burst when a
  slot is found. This way, Resy sees the user's account as having normal activity
  (browse → book), not a bot polling every 30 seconds.
- Timing jitter: monitor polls vary ±5s instead of exact 30s intervals.
- Snipe mode authenticates at T-60s and the user token is only active for ~60s,
  indistinguishable from a user who opened the page right before the drop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time as time_module
import uuid
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

import httpx

from tablement.api import ResyApiClient
from tablement.fingerprint import fingerprint_pool
from tablement.models import DropTime, SnipeConfig, SnipeResult, TimePreference
from tablement.scheduler import PrecisionScheduler
from tablement.selector import SlotSelector
from tablement.web import db
from tablement.web.deps import get_user_id
from tablement.web.encryption import decrypt_password
from tablement.web.schemas import (
    BatchReservationRequest,
    BatchReservationResponse,
    ReservationCreateRequest,
    ReservationListResponse,
    ReservationOut,
    SnipeResultOut,
)
from tablement.web.ip_pool import ip_pool
from tablement.web.ratelimit import limiter
from tablement.web.state import JobState, SnipePhase

logger = logging.getLogger(__name__)
router = APIRouter()


async def _cleanup_claims(reservation_id: str, terminal_status: str) -> None:
    """Update slot_claims when a reservation reaches a terminal state.

    Maps reservation status → claim status:
      confirmed → completed
      failed / cancelled / dry_run → cancelled
    Fire-and-forget safe — never raises.
    """
    claim_status = "completed" if terminal_status == "confirmed" else "cancelled"
    try:
        n = await db.update_claims_by_reservation(str(reservation_id), claim_status)
        if n:
            logger.debug("Cleaned %d claim(s) for reservation %s → %s", n, reservation_id, claim_status)
    except Exception as e:
        logger.debug("Claim cleanup skipped for %s: %s", reservation_id, e)


# Pre-drop polling: start hammering find_slots() this many seconds BEFORE drop
PRE_DROP_POLL_SECONDS = 2.0
# Polling interval range (jittered to avoid detection)
PRE_DROP_POLL_MIN_MS = 40   # ~25 req/s at fastest
PRE_DROP_POLL_MAX_MS = 60   # ~17 req/s at slowest, avg ~20 req/s

# Legacy pre-fire offset (still used for busy-wait precision entry)
PRE_FIRE_MS = 200

# Warmup timing (Phase 6: earlier warmup)
WARMUP_BEFORE_SECONDS = 30  # Was 5s — earlier warmup for better TCP window
PING_INTERVAL_SECONDS = 10  # Was 15s — more frequent keep-alive

# Parallel booking: get_details on top N matching slots simultaneously
PARALLEL_DETAILS_SLOTS = 3

# Legacy monitor polling (kept for backward compat, not used by new tiered loop)
MONITOR_INTERVAL_BASE = 30
MONITOR_JITTER_SECONDS = 5

# ---------------------------------------------------------------------------
# Cancellation sniping — tiered polling with rest periods
# ---------------------------------------------------------------------------

# Two tiers only (no cool tier)
CANCEL_TIER_HOT = (2, 5)            # seconds — 24-48h before reservation, or "earliest" priority
CANCEL_TIER_WARM = (5, 15)          # seconds — all other active polling

# Rest periods: pause polling to look more natural
CANCEL_REST_HOT = (60, 180)         # 1-3 min rest after hot burst
CANCEL_REST_WARM = (180, 300)       # 3-5 min rest after warm burst
CANCEL_BURST_HOT = (25 * 60, 40 * 60)   # 25-40 min burst before rest (hot)
CANCEL_BURST_WARM = (25 * 60, 35 * 60)  # 25-35 min burst before rest (warm)

# Active hours (ET)
CANCEL_ACTIVE_START = 9             # 9 AM ET — start polling
CANCEL_ACTIVE_END = 22              # 10 PM ET — start slowing down
CANCEL_SLEEP_START = 1              # 1 AM ET — guaranteed no polling
CANCEL_SLEEP_END = 9                # 9 AM ET — resume polling

# Late-night slowdown: 10 PM–midnight, add extra seconds to interval
CANCEL_SLOWDOWN_EXTRA = (3, 8)      # add 3-8s to whatever tier interval


# ---------------------------------------------------------------------------
# Auto-scout — automatically start/stop scouts based on active snipes
# ---------------------------------------------------------------------------


async def _ensure_scout_for_venue(app, venue_id: int, venue_name: str = "") -> None:
    """Auto-start a drop scout ONLY when 2+ snipes target the same venue.

    With 1 snipe, the snipe polls on its own — no scout overhead.
    With 2+ snipes, a shared scout polls once and broadcasts to all subscribers,
    saving duplicate requests and giving instant detection to everyone.

    Also a no-op if a scout is already running (e.g. manually started from admin).
    """
    scout = getattr(app.state, "scout", None)
    if not scout:
        return

    # Check if scout already running — no-op
    from tablement.web.scout import _task_key
    key = _task_key(venue_id, "drop")
    if key in scout._tasks and not scout._tasks[key].done():
        logger.debug("Scout already running for venue %d", venue_id)
        return

    # Only auto-start if 2+ active snipes for this venue
    active_count = await db.count_active_snipes_for_venue(venue_id)
    if active_count < 2:
        logger.debug("Only %d snipe(s) for venue %d — no scout needed", active_count, venue_id)
        return

    # Look up curated restaurant for drop time config
    curated = await db.get_curated_restaurant(venue_id)
    config = {}
    if curated:
        venue_name = venue_name or curated.get("name", "")
        if curated.get("drop_hour") is not None:
            config["drop_hour"] = curated["drop_hour"]
        if curated.get("drop_minute") is not None:
            config["drop_minute"] = curated["drop_minute"]
        if curated.get("drop_days_ahead"):
            config["days_ahead"] = curated["drop_days_ahead"]
    else:
        config["drop_hour"] = 9
        config["drop_minute"] = 0

    try:
        await scout.start_scout(venue_id, venue_name, campaign_type="drop", auto=True, **config)
        logger.info("Auto-started drop scout for %s (%d) — %d snipes targeting this venue",
                     venue_name or venue_id, venue_id, active_count)
    except Exception as e:
        logger.warning("Failed to auto-start scout for venue %d: %s", venue_id, e)


async def _maybe_stop_scout_for_venue(app, venue_id: int) -> None:
    """Stop an AUTO-STARTED scout when snipe count drops below 2.

    Only stops scouts that were auto-started. Manually started scouts
    (from admin panel) are never auto-stopped — admin controls those.
    """
    scout = getattr(app.state, "scout", None)
    if not scout:
        return

    # Only auto-stop auto-started scouts
    if not scout.is_auto_started(venue_id, "drop"):
        return

    # Check if enough snipes remain to justify the scout
    remaining = await db.count_active_snipes_for_venue(venue_id)
    if remaining >= 2:
        logger.debug("Scout for venue %d: %d snipes still active, keeping scout", venue_id, remaining)
        return

    # Less than 2 snipes — scout no longer needed
    from tablement.web.scout import _task_key
    key = _task_key(venue_id, "drop")
    if key in scout._tasks and not scout._tasks[key].done():
        logger.info("Auto-stopping scout for venue %d — only %d snipe(s) remain", venue_id, remaining)
        await scout.stop_scout(venue_id, campaign_type="drop")


# ---------------------------------------------------------------------------
# SnipeIntelBuffer — accumulates every poll response for batch DB insert
# ---------------------------------------------------------------------------


class SnipeIntelBuffer:
    """In-memory accumulator for inline poll snapshots during a snipe.

    Every find_slots() response is recorded as a dict. After the snipe
    completes, the buffer is batch-flushed to Supabase in a single insert.
    Zero impact on the booking hot path — just a list.append() per poll.

    Typical size: 100-500 entries (3-15s at ~33 req/s), ~100KB in memory.
    """

    def __init__(self, venue_id: int, target_date: str, reservation_id: str | None = None):
        self.venue_id = venue_id
        self.target_date = target_date
        self.reservation_id = reservation_id
        self._entries: list[dict] = []
        self._start_mono = time_module.monotonic()
        self._drop_mono: float | None = None  # monotonic time when drop detected

    def record(self, slot_count: int, slots: list | None = None) -> None:
        """Record a poll response. Call after every find_slots()."""
        now_mono = time_module.monotonic()
        ms_since_start = (now_mono - self._start_mono) * 1000
        ms_since_drop = (
            (now_mono - self._drop_mono) * 1000
            if self._drop_mono is not None
            else None
        )

        entry = {
            "venue_id": self.venue_id,
            "target_date": self.target_date,
            "slot_count": slot_count,
            "ms_since_start": round(ms_since_start, 1),
            "ms_since_drop": round(ms_since_drop, 1) if ms_since_drop is not None else None,
            "source": "snipe_inline",
        }
        if self.reservation_id:
            entry["reservation_id"] = self.reservation_id

        # Only include full slot data when slots > 0 (saves space)
        if slot_count > 0 and slots:
            entry["slots_json"] = [
                {"time": s.date.start, "type": s.config.type}
                for s in slots[:50]  # Cap at 50 to avoid huge payloads
            ]

        self._entries.append(entry)

    def mark_drop(self) -> None:
        """Mark the exact moment slots transitioned from 0 → N."""
        self._drop_mono = time_module.monotonic()

    async def flush(self) -> int:
        """Batch-insert all entries to Supabase. Call after snipe completes."""
        if not self._entries:
            return 0
        count = await db.batch_insert_slot_snapshots(self._entries)
        logger.info(
            "Flushed %d inline poll snapshots for venue %d (reservation %s)",
            count, self.venue_id, self.reservation_id or "N/A",
        )
        return count

    @property
    def count(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Error sanitization — generic messages for clients, raw details in server logs
# ---------------------------------------------------------------------------


def _safe_error(e: Exception) -> str:
    """Map exception types to generic user-facing messages."""
    if isinstance(e, httpx.HTTPStatusError):
        return "Resy returned an error"
    if isinstance(e, httpx.ConnectError):
        return "Could not connect to Resy"
    if isinstance(e, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "Request timed out"
    if isinstance(e, asyncio.CancelledError):
        return "Cancelled"
    return "An unexpected error occurred"


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


@router.post("/", response_model=ReservationOut)
@limiter.limit("10/minute")
async def create_reservation(
    body: ReservationCreateRequest,
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """Create a new reservation job (snipe or monitor)."""
    # Validate: snipe mode requires drop_time
    if body.mode == "snipe" and not body.drop_time:
        raise HTTPException(400, "Snipe mode requires drop_time configuration")

    # Validate: drop time must be in the future
    if body.mode == "snipe" and body.drop_time:
        from tablement.scheduler import PrecisionScheduler
        _sched = PrecisionScheduler()
        _config = SnipeConfig(
            venue_id=body.venue_id,
            date=date.fromisoformat(body.date) if isinstance(body.date, str) else body.date,
            party_size=body.party_size,
            time_preferences=[],
            drop_time=DropTime(
                hour=body.drop_time.hour,
                minute=body.drop_time.minute,
                days_ahead=body.drop_time.days_ahead,
            ),
        )
        _drop_dt = _sched.calculate_drop_datetime(_config)
        if _drop_dt < datetime.now(_drop_dt.tzinfo):
            raise HTTPException(400, "Drop time has already passed")
        _minutes_until = (_drop_dt - datetime.now(_drop_dt.tzinfo)).total_seconds() / 60
        if _minutes_until < 10:
            raise HTTPException(
                400,
                f"Drop is only {_minutes_until:.0f} minutes away — minimum 10 minutes needed for warmup",
            )

    # Check user has linked Resy account
    profile = await db.get_profile(user_id)
    if not profile or not profile.get("resy_email"):
        raise HTTPException(400, "Please link your Resy account first (POST /api/auth/link-resy)")

    # Check for duplicate reservation (same user + venue + date)
    target_date_str = body.date if isinstance(body.date, str) else body.date.isoformat()
    if await db.has_active_reservation(user_id, body.venue_id, target_date_str):
        raise HTTPException(409, "You already have an active reservation for this venue and date")

    # Per-user reservation limits
    if body.mode == "snipe":
        count = await db.count_active_drop_snipes(user_id)
        if count >= 4:
            raise HTTPException(429, "Maximum 4 active drop snipes allowed")
    else:
        count = await db.count_active_monitors(user_id)
        if count >= 5:
            raise HTTPException(429, "Maximum 5 active cancellation monitors allowed")

    # Build time_preferences and drop_time as JSON for DB
    time_prefs_json = [tp.model_dump() for tp in body.time_preferences]
    drop_time_json = body.drop_time.model_dump() if body.drop_time else None

    # Determine initial status
    initial_status = "scheduled" if body.mode == "snipe" else "monitoring"

    # Create DB row
    row = await db.create_reservation(
        user_id=user_id,
        venue_id=body.venue_id,
        venue_name=body.venue_name,
        party_size=body.party_size,
        target_date=body.date,
        mode=body.mode,
        status=initial_status,
        time_preferences=time_prefs_json,
        drop_time_config=drop_time_json,
    )

    reservation_id = row["id"]

    # Schedule the job
    if body.mode == "snipe":
        _schedule_snipe(request.app, reservation_id, user_id, body, profile, body.dry_run)
    else:
        _schedule_monitor(request.app, reservation_id, user_id, body, profile)

    return _row_to_out(row)


@router.post("/batch", response_model=BatchReservationResponse)
@limiter.limit("10/minute")
async def create_batch_reservations(
    body: BatchReservationRequest,
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """Create multiple reservation jobs for the smart calendar.

    One reservation per date, all sharing a group_id. Released dates become
    cancellation snipes (monitor mode with tiered polling). Unreleased dates
    become release snipes (scheduled at drop time).
    """
    # Pre-flight rate limit check
    snipe_count = await db.count_active_drop_snipes(user_id)
    monitor_count = await db.count_active_monitors(user_id)
    snipe_dates_in_batch = len([d for d in body.dates if date.fromisoformat(d) > date.today() + timedelta(days=body.drop_time.days_ahead if body.drop_time else 30)])
    monitor_dates_in_batch = len(body.dates) - snipe_dates_in_batch
    if snipe_count + snipe_dates_in_batch > 4:
        raise HTTPException(429, f"Would exceed max 4 drop snipes (currently {snipe_count} active)")
    if monitor_count + monitor_dates_in_batch > 5:
        raise HTTPException(429, f"Would exceed max 5 cancellation monitors (currently {monitor_count} active)")

    # Check user has linked Resy account
    profile = await db.get_profile(user_id)
    if not profile or not profile.get("resy_email"):
        raise HTTPException(400, "Please link your Resy account first")

    if not body.time_preferences:
        raise HTTPException(400, "At least one time preference is required")

    # Validate: if any dates are unreleased (snipe mode), drop time must be in the future
    if body.drop_time:
        from tablement.scheduler import PrecisionScheduler
        _sched = PrecisionScheduler()
        _any_date = date.fromisoformat(body.dates[0])
        _config = SnipeConfig(
            venue_id=body.venue_id,
            date=_any_date,
            party_size=body.party_size,
            time_preferences=[],
            drop_time=DropTime(
                hour=body.drop_time.hour,
                minute=body.drop_time.minute,
                days_ahead=body.drop_time.days_ahead,
            ),
        )
        _drop_dt = _sched.calculate_drop_datetime(_config)
        days_ahead_val = body.drop_time.days_ahead
        today = date.today()
        has_unreleased = any(
            date.fromisoformat(d) > today + timedelta(days=days_ahead_val)
            for d in body.dates
        )
        if has_unreleased and _drop_dt < datetime.now(_drop_dt.tzinfo):
            raise HTTPException(400, "Drop time has already passed for unreleased dates")

    # Generate group ID to link all reservations
    group_id = str(uuid.uuid4())
    time_prefs_json = [tp.model_dump() for tp in body.time_preferences]
    drop_time_json = body.drop_time.model_dump() if body.drop_time else None

    # Determine the released/unreleased boundary
    # If we have drop policy info, compute last released date
    days_ahead = body.drop_time.days_ahead if body.drop_time else 30
    today = date.today()
    last_released = today + timedelta(days=days_ahead)

    reservation_ids = []
    for date_str in body.dates:
        target = date.fromisoformat(date_str)
        is_released = target <= last_released

        if is_released:
            # Released date → cancellation sniping (monitor mode with tiered polling)
            mode = "monitor"
            initial_status = "monitoring"
        else:
            # Unreleased date → release sniping (one-shot at drop time)
            mode = "snipe"
            initial_status = "scheduled"
            if not body.drop_time:
                raise HTTPException(400, f"Drop time required for unreleased date {date_str}")

        # Determine polling tier for cancellation snipes
        poll_tier = "warm"
        if is_released:
            hours_until = (datetime.combine(target, time(19, 0)) - datetime.now()).total_seconds() / 3600
            if 24 <= hours_until <= 48:
                poll_tier = "hot"
            elif body.book_earliest:
                poll_tier = "hot"  # First date in "earliest" mode gets hot tier

        # Skip duplicate dates for this user+venue
        if await db.has_active_reservation(user_id, body.venue_id, date_str):
            logger.info("Batch: skipping duplicate reservation for venue %d date %s", body.venue_id, date_str)
            continue

        row = await db.create_reservation(
            user_id=user_id,
            venue_id=body.venue_id,
            venue_name=body.venue_name,
            party_size=body.party_size,
            target_date=date_str,
            mode=mode,
            status=initial_status,
            time_preferences=time_prefs_json,
            drop_time_config=drop_time_json,
            group_id=group_id,
            book_earliest=body.book_earliest,
            latest_notify_hours=body.latest_notify_hours,
            poll_tier=poll_tier,
            notification_email=body.notification_email,
        )
        reservation_id = row["id"]
        reservation_ids.append(reservation_id)

        # Register slot claims for conflict detection
        for tp in body.time_preferences:
            try:
                await db.create_slot_claim(
                    user_id=user_id,
                    venue_id=body.venue_id,
                    venue_name=body.venue_name,
                    target_date=date_str,
                    preferred_time=tp.time,
                    seating_type=tp.seating_type or "",
                    reservation_id=str(reservation_id),
                )
            except Exception as claim_err:
                # Non-fatal — duplicate claim or DB error shouldn't block the snipe
                logger.debug("Slot claim create skipped: %s", claim_err)

        # Schedule the job
        if mode == "snipe":
            _schedule_snipe(request.app, reservation_id, user_id,
                            _mock_create_request(body, date_str, mode), profile, dry_run=False)
        else:
            _schedule_cancellation_snipe(
                request.app, reservation_id, user_id,
                _mock_create_request(body, date_str, mode), profile,
                group_id=group_id,
                poll_tier=poll_tier,
                latest_notify_hours=body.latest_notify_hours,
            )

    return BatchReservationResponse(
        group_id=group_id,
        reservation_ids=reservation_ids,
        count=len(reservation_ids),
    )


def _mock_create_request(body: BatchReservationRequest, date_str: str, mode: str) -> ReservationCreateRequest:
    """Build a ReservationCreateRequest from a batch request for a single date."""
    return ReservationCreateRequest(
        venue_id=body.venue_id,
        venue_name=body.venue_name,
        party_size=body.party_size,
        date=date_str,
        mode=mode,
        time_preferences=body.time_preferences,
        drop_time=body.drop_time,
    )


@router.get("/", response_model=ReservationListResponse)
async def list_reservations(user_id: str = Depends(get_user_id)):
    """List all reservations for the current user."""
    rows = await db.list_reservations(user_id)
    return ReservationListResponse(reservations=[_row_to_out(r) for r in rows])


@router.get("/{reservation_id}", response_model=ReservationOut)
async def get_reservation(
    reservation_id: str,
    user_id: str = Depends(get_user_id),
):
    """Get a single reservation's status."""
    row = await db.get_reservation(reservation_id, user_id)
    if not row:
        raise HTTPException(404, "Reservation not found")
    return _row_to_out(row)


@router.post("/{reservation_id}/cancel")
async def cancel_reservation(
    reservation_id: str,
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """Cancel a reservation job."""
    row = await db.get_reservation(reservation_id, user_id)
    if not row:
        raise HTTPException(404, "Reservation not found")

    if row["status"] in ("confirmed", "cancelled"):
        raise HTTPException(400, f"Cannot cancel reservation with status '{row['status']}'")

    # Cancel in-memory job
    job_manager = request.app.state.jobs
    job = job_manager.get(reservation_id)
    if job:
        job.broadcast("snipe_result", {"success": False, "error": "Cancelled by user"})
        job_manager.remove(reservation_id)

    # Cancel APScheduler job if exists
    scheduler = request.app.state.scheduler
    if scheduler:
        try:
            scheduler.remove_job(f"snipe_{reservation_id}")
        except Exception:
            pass
        try:
            scheduler.remove_job(f"monitor_{reservation_id}")
        except Exception:
            pass

    # Update DB
    await db.update_reservation(reservation_id, status="cancelled", error="Cancelled by user")
    await _cleanup_claims(reservation_id, "cancelled")

    # Auto-stop scout if this was the last snipe for the venue
    try:
        await _maybe_stop_scout_for_venue(request.app, row["venue_id"])
    except Exception:
        pass

    # Release Stripe hold if exists
    stripe_pi = row.get("stripe_payment_intent_id")
    if stripe_pi:
        try:
            import stripe

            stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
            stripe.PaymentIntent.cancel(stripe_pi)
            logger.info("Released Stripe hold for %s", reservation_id)
        except Exception as e:
            logger.warning("Failed to release Stripe hold: %s", e)

    return {"cancelled": True}


@router.get("/{reservation_id}/events")
async def events(
    reservation_id: str,
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """SSE stream for real-time updates on a reservation."""
    # Verify ownership
    row = await db.get_reservation(reservation_id, user_id)
    if not row:
        raise HTTPException(404, "Reservation not found")

    job_manager = request.app.state.jobs
    job = job_manager.get(reservation_id)

    # If no active job, send current DB state and close
    if not job:
        async def static_response():
            yield {
                "event": "snipe_phase",
                "data": json.dumps({
                    "phase": row.get("status", "idle"),
                    "message": row.get("error", ""),
                    "attempt": row.get("attempts", 0),
                }),
            }

        return EventSourceResponse(static_response())

    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    job.event_queues.append(queue)

    async def generate():
        try:
            # Send current state on connect
            s = job.status
            yield {
                "event": "snipe_phase",
                "data": json.dumps({
                    "phase": s.phase.value,
                    "message": s.message,
                    "attempt": s.attempt,
                }),
            }

            idle_since = time_module.monotonic()
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Send heartbeat to keep connection alive
                    yield {"event": "heartbeat", "data": "{}"}
                    if time_module.monotonic() - idle_since > 300:
                        # 5 min with no real activity — close
                        break
                    continue

                idle_since = time_module.monotonic()
                yield {
                    "event": msg["event"],
                    "data": json.dumps(msg["data"]),
                }
                if msg["event"] == "snipe_result":
                    break
        except asyncio.CancelledError:
            pass
        finally:
            if queue in job.event_queues:
                job.event_queues.remove(queue)

    return EventSourceResponse(generate())


# ---------------------------------------------------------------------------
# Job scheduling helpers
# ---------------------------------------------------------------------------


def _schedule_snipe(app, reservation_id: str, user_id: str, body, profile: dict, dry_run: bool):
    """Schedule a snipe job via APScheduler or run immediately if drop is past."""
    scheduler = app.state.scheduler

    # Auto-start a drop scout for this venue (idempotent — no-op if already running)
    asyncio.create_task(
        _ensure_scout_for_venue(app, body.venue_id, body.venue_name)
    )

    # Build SnipeConfig
    config = _build_snipe_config(body)
    precision_scheduler = PrecisionScheduler()
    drop_dt = precision_scheduler.calculate_drop_datetime(config)

    now = datetime.now(drop_dt.tzinfo)
    seconds_until_drop = (drop_dt - now).total_seconds()

    if seconds_until_drop <= 60:
        # Drop is imminent or past — run now
        job = app.state.jobs.create(reservation_id, user_id)
        job.task = asyncio.create_task(
            _run_snipe(app, reservation_id, user_id, config, profile, dry_run, job)
        )
    elif scheduler:
        # Schedule to start at T-60s
        run_at = drop_dt - timedelta(seconds=60)
        scheduler.add_job(
            _run_snipe_from_scheduler,
            trigger="date",
            run_date=run_at,
            id=f"snipe_{reservation_id}",
            replace_existing=True,
            args=[app, reservation_id, user_id, config, profile, dry_run],
        )
        logger.info("Snipe %s scheduled for %s", reservation_id, run_at.isoformat())
    else:
        # No scheduler — run as background task with sleep
        job = app.state.jobs.create(reservation_id, user_id)
        job.task = asyncio.create_task(
            _run_snipe(app, reservation_id, user_id, config, profile, dry_run, job)
        )


def _schedule_monitor(app, reservation_id: str, user_id: str, body, profile: dict):
    """Schedule a monitor job — uses new tiered cancellation sniping loop.

    Legacy entry point for single-date monitor creation via POST /reservations/.
    New batch endpoint uses _schedule_cancellation_snipe() directly.
    """
    _schedule_cancellation_snipe(
        app, reservation_id, user_id, body, profile,
        group_id=None, poll_tier="warm", latest_notify_hours=2.0,
    )


def _schedule_cancellation_snipe(
    app, reservation_id: str, user_id: str, body, profile: dict,
    *, group_id: str | None, poll_tier: str, latest_notify_hours: float,
):
    """Launch a tiered cancellation sniping loop as an asyncio task.

    Uses a pure asyncio loop (not APScheduler) for fine-grained control
    over burst/rest cycles, tier switching, and sleep-hour detection.
    """
    config = _build_snipe_config(body)
    job = app.state.jobs.create(reservation_id, user_id)
    job.status.phase = SnipePhase.MONITORING
    job.status.message = f"Watching for cancellations ({poll_tier} tier)"
    job.task = asyncio.create_task(
        _cancellation_snipe_loop(
            app, reservation_id, user_id, config, profile, job,
            group_id=group_id,
            poll_tier=poll_tier,
            latest_notify_hours=latest_notify_hours,
        )
    )
    logger.info(
        "Cancellation snipe %s started (tier=%s, group=%s)",
        reservation_id, poll_tier, group_id or "none",
    )


# ---------------------------------------------------------------------------
# Snipe execution (adapted from routes/snipe.py)
# ---------------------------------------------------------------------------


async def _run_snipe_from_scheduler(
    app, reservation_id: str, user_id: str, config: SnipeConfig, profile: dict, dry_run: bool,
):
    """Entry point when APScheduler fires the job at T-60s."""
    job = app.state.jobs.create(reservation_id, user_id)
    await _run_snipe(app, reservation_id, user_id, config, profile, dry_run, job)


async def _run_snipe(
    app,
    reservation_id: str,
    user_id: str,
    config: SnipeConfig,
    profile: dict,
    dry_run: bool,
    job: JobState,
):
    """Execute the full snipe flow for a reservation.

    Uses the same optimized single-connection strategy:
    auth → keep-alive → warmup → pre-fire → snipe loop.

    Anti-detection: The user's Resy token is only active for ~60 seconds total
    (T-60s auth → T-0 snipe → result). From Resy's perspective this looks like
    a user who opened the page right before the reservation drop.
    """
    scheduler = PrecisionScheduler()
    selector = SlotSelector(window_minutes=config.window_minutes)
    drop_dt = scheduler.calculate_drop_datetime(config)
    day_str = config.date.isoformat()

    def _set_phase(phase: SnipePhase, message: str, **kw):
        job.status.phase = phase
        job.status.message = message
        for k, v in kw.items():
            setattr(job.status, k, v)
        job.broadcast("snipe_phase", {"phase": phase.value, "message": message, **kw})

    # Assign a dedicated IP for this snipe (multi-EIP distribution)
    snipe_ip_key = f"snipe-{user_id}-{reservation_id}"
    local_ip = ip_pool.get_ip(snipe_ip_key)
    logger.info("Snipe %s: bound to IP %s", reservation_id, local_ip)

    try:
        # Non-blocking NTP offset check (runs in thread, saves 2-5s)
        ntp_task = asyncio.create_task(scheduler.check_ntp_offset_async())

        _set_phase(SnipePhase.WAITING, f"Waiting until {drop_dt.strftime('%H:%M:%S %Z')}")
        await db.update_reservation(reservation_id, status="scheduled")

        # Decrypt Resy credentials
        resy_email = profile["resy_email"]
        resy_password = decrypt_password(profile["resy_password_encrypted"])

        # Collect NTP result
        ntp_offset = await ntp_task
        if ntp_offset is not None:
            logger.info("NTP offset: %.1fms", ntp_offset * 1000)

        # Apply NTP compensation
        drop_dt = scheduler.compensate_drop_time(drop_dt)

        # Check if a drop scout is watching this venue — subscribe for instant notification
        scout = getattr(app.state, "scout", None)
        scout_event = scout.subscribe_drop(config.venue_id) if scout else None
        if scout_event:
            logger.info("Snipe %s: subscribed to scout for venue %d", reservation_id, config.venue_id)

        # Single persistent connection with per-user fingerprint + proxy + dedicated IP
        async with ResyApiClient(user_id=user_id, local_address=local_ip) as client:
            # Wait until T-60 then authenticate
            auth_time = drop_dt - timedelta(seconds=60)
            now = datetime.now(drop_dt.tzinfo)
            wait_secs = (auth_time - now).total_seconds()

            # Countdown until T-60
            while wait_secs > 0:
                remaining = (drop_dt - datetime.now(drop_dt.tzinfo)).total_seconds()
                job.broadcast("countdown_tick", {
                    "seconds_remaining": remaining,
                    "phase": "waiting",
                })
                sleep_time = min(wait_secs, 1.0)
                await asyncio.sleep(sleep_time)
                wait_secs = (auth_time - datetime.now(drop_dt.tzinfo)).total_seconds()

            # Authenticate at T-60 (with retry)
            _set_phase(SnipePhase.AUTHENTICATING, "Authenticating with Resy...")
            for _auth_attempt in range(3):
                try:
                    auth_resp = await client.authenticate(resy_email, resy_password)
                    break
                except Exception as auth_err:
                    if _auth_attempt < 2:
                        logger.warning("Auth attempt %d failed for %s: %s — retrying in 2s", _auth_attempt + 1, reservation_id, auth_err)
                        await asyncio.sleep(2)
                    else:
                        raise
            pm = next(
                (p for p in auth_resp.payment_methods if p.is_default),
                auth_resp.payment_methods[0],
            )
            token = auth_resp.token
            payment_id = pm.id

            client.set_auth_token(token)

            # Pre-build booking template (freeze headers + static params)
            client.prepare_book_template(payment_id)
            logger.info("Auth complete, book template prepared")

            # Keep-alive pings until T-30 (was T-5)
            warmup_time = drop_dt - timedelta(seconds=WARMUP_BEFORE_SECONDS)
            last_ping = time_module.monotonic()
            while True:
                now = datetime.now(drop_dt.tzinfo)
                remaining = (drop_dt - now).total_seconds()
                if (warmup_time - now).total_seconds() <= 0:
                    break
                job.broadcast("countdown_tick", {
                    "seconds_remaining": remaining,
                    "phase": "authenticating",
                })
                if time_module.monotonic() - last_ping > PING_INTERVAL_SECONDS:
                    await client.ping()
                    last_ping = time_module.monotonic()
                await asyncio.sleep(min((warmup_time - now).total_seconds(), 1.0))

            # Warmup at T-30: proxy connection + Resy clock cal + direct client
            _set_phase(SnipePhase.WARMING, "Warming connections + calibrating clock...")
            warmup_results = await asyncio.gather(
                client.find_slots(config.venue_id, day_str, config.party_size),
                client.calibrate_resy_clock(samples=3),
                client.warmup_direct(),
                return_exceptions=True,
            )

            # Apply Resy clock offset (reject outliers >±500ms)
            resy_offset = warmup_results[1] if isinstance(warmup_results[1], float) else 0.0
            if abs(resy_offset) > 0.5:
                logger.warning("Resy clock offset %.1fms exceeds 500ms — ignoring", resy_offset * 1000)
                resy_offset = 0.0
            if resy_offset != 0.0:
                scheduler.set_resy_offset(resy_offset)
                drop_dt = scheduler.compensate_drop_time(
                    scheduler.calculate_drop_datetime(config)
                )

            # Imperva pre-warm: ensure both proxy + direct clients have
            # valid Imperva cookies (visid_incap, nlbi, incap_ses) before
            # entering the critical booking window. Also measures latency.
            imperva_status = {}
            latency_data = {}
            try:
                imperva_status, latency_data = await asyncio.gather(
                    client.prewarm_imperva(),
                    client.measure_latency(samples=3),
                )
                logger.info(
                    "Imperva cookies: proxy=%d, direct=%s | Latency: proxy=%s, direct=%s",
                    imperva_status.get("proxy_client", {}).get("imperva_cookies", 0),
                    imperva_status.get("direct_client", {}).get("imperva_cookies", "N/A"),
                    latency_data.get("proxy_ms", "?"),
                    latency_data.get("direct_ms", "?"),
                )
            except Exception as e:
                logger.warning("Imperva pre-warm / latency check failed (non-critical): %s", e)

            # Continue pinging until pre-drop polling window
            poll_entry = drop_dt - timedelta(seconds=PRE_DROP_POLL_SECONDS)
            while True:
                now = datetime.now(drop_dt.tzinfo)
                remaining = (drop_dt - now).total_seconds()
                if (poll_entry - now).total_seconds() <= 0:
                    break
                job.broadcast("countdown_tick", {
                    "seconds_remaining": remaining,
                    "phase": "warming",
                })
                if time_module.monotonic() - last_ping > PING_INTERVAL_SECONDS:
                    await client.ping()
                    last_ping = time_module.monotonic()
                await asyncio.sleep(min((poll_entry - now).total_seconds(), 1.0))

            # Switch to snipe mode (tighter timeouts)
            client.set_snipe_mode(True)

            # --- SCOUT SUBSCRIPTION: wait for scout to detect drop instead of own polling ---
            # If a scout is actively watching this venue, we skip our own pre-drop polling
            # and wait for the scout to broadcast. When it fires, we jump straight to booking.
            # This saves N-1 polling connections when N users snipe the same venue.
            scout_detected = False
            if scout_event is not None:
                # Race condition fix: check if scout already detected drop before we got here
                if scout_event.is_set():
                    logger.info("Snipe %s: scout ALREADY detected drop — booking immediately!", reservation_id)
                    scout_detected = True
                else:
                    _set_phase(SnipePhase.SNIPING, "Scout monitoring — waiting for drop...")
                    asyncio.create_task(db.update_reservation(reservation_id, status="sniping"))
                    try:
                        # Wait for scout with a timeout (drop window + 5 min safety)
                        timeout_s = max(
                            (drop_dt + timedelta(minutes=5) - datetime.now(drop_dt.tzinfo)).total_seconds(),
                            60,
                        )
                        await asyncio.wait_for(scout_event.wait(), timeout=timeout_s)
                        logger.info("Snipe %s: SCOUT DETECTED DROP — booking immediately!", reservation_id)
                        scout_detected = True
                    except asyncio.TimeoutError:
                        logger.warning("Snipe %s: scout timeout — falling back to own polling", reservation_id)
                        scout_event = None  # Fall through to regular polling

            if scout_detected:
                # Scout detected slots — skip polling, go straight to booking
                _set_phase(SnipePhase.SNIPING, "Scout detected drop — booking...")
                asyncio.create_task(db.update_reservation(reservation_id, status="sniping"))

                # Get the slots the scout found and select best match
                scout_slots_data = scout.get_drop_slots(config.venue_id) if scout else []
                logger.info(
                    "Snipe %s: scout provided %d slots, proceeding to book",
                    reservation_id, len(scout_slots_data),
                )

                # Still need to find_slots ourselves to get proper Slot objects with config tokens
                intel_buffer = SnipeIntelBuffer(
                    venue_id=config.venue_id,
                    target_date=day_str,
                    reservation_id=reservation_id,
                )

                # Use the snipe loop but it should find slots on the first poll
                result = await _snipe_loop(
                    client, config, selector, payment_id, dry_run, job,
                    pre_drop_poll=True,
                    intel_buffer=intel_buffer,
                )
            else:
                # No scout or scout timed out — do our own pre-drop polling
                # AGGRESSIVE PRE-DROP POLLING
                # Start hammering find_slots() before drop. Jittered intervals
                # (20-40ms) to avoid pattern detection. Slots will return empty
                # until the exact moment Resy releases them.
                _set_phase(SnipePhase.SNIPING, f"Pre-drop polling (T-{PRE_DROP_POLL_SECONDS}s)...")
                # Non-blocking: status update is informational, don't block the snipe loop
                asyncio.create_task(db.update_reservation(reservation_id, status="sniping"))

                # Create intel buffer to record EVERY poll response
                intel_buffer = SnipeIntelBuffer(
                    venue_id=config.venue_id,
                    target_date=day_str,
                    reservation_id=reservation_id,
                )

                result = await _snipe_loop(
                    client, config, selector, payment_id, dry_run, job,
                    pre_drop_poll=True,
                    intel_buffer=intel_buffer,
                )

            # ---- DROP INTELLIGENCE: record observation from real snipe ----
            # This piggybacks on the real snipe — zero extra polling.
            # We know exactly when slots appeared because the snipe loop tracked it.
            try:
                if result._drop_detected_at is not None:
                    drop_offset = (result._drop_detected_at - drop_dt).total_seconds() * 1000
                    booking_elapsed = None
                    if result.success and result.elapsed_seconds:
                        # Time from slots appearing to booking confirmed
                        booking_elapsed = (result.elapsed_seconds * 1000) - (
                            result._drop_detected_at - datetime.now(drop_dt.tzinfo)
                        ).total_seconds() * -1000 if result._drop_detected_at else None
                    observation = {
                        "venue_id": config.venue_id,
                        "venue_name": config.venue_name if hasattr(config, "venue_name") else "",
                        "target_date": day_str,
                        "expected_drop_at": drop_dt.isoformat(),
                        "actual_drop_at": result._drop_detected_at.isoformat(),
                        "offset_ms": round(drop_offset, 1),
                        "slots_found": result._slots_at_drop or 0,
                        "slot_types": result._slot_types_at_drop or [],
                        "first_slot_time": result._first_slot_time,
                        "last_slot_time": result._last_slot_time,
                        "poll_attempt": result._drop_poll_attempt or 0,
                        "poll_interval_ms": result._avg_poll_interval_ms or 0,
                        "resy_clock_offset_ms": round(resy_offset * 1000, 1) if resy_offset else None,
                        "source": "snipe",
                        "booking_result": (
                            "dry_run" if dry_run else (
                                "booked" if result.success else "failed"
                            )
                        ),
                        "booking_elapsed_ms": round(result.elapsed_seconds * 1000, 1) if result.elapsed_seconds else None,
                        # Imperva + latency data from warmup phase
                        "proxy_latency_ms": latency_data.get("proxy_ms"),
                        "direct_latency_ms": latency_data.get("direct_ms"),
                        "imperva_cookies_count": imperva_status.get("proxy_client", {}).get("imperva_cookies", 0),
                    }
                    await db.insert_drop_observation(observation)
                    logger.info(
                        "Drop intel recorded from snipe: %s offset=%.1fms slots=%d",
                        config.venue_id, drop_offset, result._slots_at_drop or 0,
                    )

                    # Kick off velocity tracking in background (non-blocking)
                    asyncio.create_task(
                        _post_snipe_velocity_tracking(
                            client, config.venue_id, day_str,
                            config.party_size, reservation_id,
                        )
                    )
            except Exception as intel_err:
                logger.warning("Drop intel recording failed (non-critical): %s", intel_err)

            # Flush inline poll buffer — non-blocking background task
            # Every single poll response from the snipe loop is saved here
            try:
                asyncio.create_task(intel_buffer.flush())
                logger.info(
                    "Queued flush of %d inline poll snapshots for venue %d",
                    intel_buffer.count, config.venue_id,
                )
            except Exception as flush_err:
                logger.warning("Intel buffer flush failed (non-critical): %s", flush_err)

            # ---- POST-TIMEOUT VERIFICATION ----
            # When the snipe loop reports failure (timeout/max attempts),
            # the booking request may have ACTUALLY succeeded on Resy's side.
            # This is the Semma scenario: our retry window expired before we
            # got the HTTP response, but Resy processed the booking.
            # Check the user's Resy reservations to verify before marking failed.
            if not result.success and not dry_run:
                try:
                    logger.info(
                        "Snipe %s reported failure (%s). "
                        "Verifying with Resy before marking as failed...",
                        reservation_id, result.error,
                    )
                    _set_phase(SnipePhase.DONE, "Verifying with Resy...")
                    job.broadcast("snipe_attempt", {
                        "attempt": result.attempts,
                        "slots_found": 0,
                        "message": "Double-checking with Resy...",
                    })

                    # Small delay — give Resy's backend time to finalize
                    await asyncio.sleep(2.0)

                    existing = await client.check_booking_exists(
                        venue_id=config.venue_id,
                        target_date=day_str,
                        party_size=config.party_size,
                    )

                    if existing:
                        logger.info(
                            "VERIFICATION SUCCESS: Booking exists on Resy "
                            "despite snipe loop timeout! Overriding to confirmed. "
                            "reservation=%s venue=%s date=%s",
                            reservation_id, config.venue_id, day_str,
                        )
                        # Override the result — the booking DID go through
                        result = SnipeResult(
                            success=True,
                            resy_token=existing.get("resy_token", "verified"),
                            slot=result.slot,
                            attempts=result.attempts,
                            elapsed_seconds=result.elapsed_seconds,
                            error=None,
                        )
                        job.broadcast("snipe_attempt", {
                            "attempt": result.attempts,
                            "slots_found": 0,
                            "message": "Booking confirmed on Resy! (verified after timeout)",
                        })
                    else:
                        logger.info(
                            "Verification: no booking found on Resy for "
                            "venue=%s date=%s — confirming failure.",
                            config.venue_id, day_str,
                        )
                except Exception as verify_err:
                    logger.warning(
                        "Post-timeout verification failed (non-critical, "
                        "keeping original result): %s", verify_err,
                    )

            # Update DB with result
            is_dry_run_success = dry_run and result.success
            update_fields = {
                "status": "dry_run" if is_dry_run_success else ("confirmed" if result.success else "failed"),
                "attempts": result.attempts,
                "elapsed_seconds": result.elapsed_seconds,
            }
            if result.resy_token:
                update_fields["resy_token"] = result.resy_token
            if result.error:
                update_fields["error"] = result.error
            await db.update_reservation(reservation_id, **update_fields)

            # Clean up slot claims on terminal state
            await _cleanup_claims(reservation_id, update_fields["status"])

            # Stripe: charge on success (fire-and-forget, never blocks booking)
            if result.success and not is_dry_run_success:
                asyncio.create_task(_handle_stripe_result(reservation_id, user_id, result))

            # Notify user (SMS / email) — fire-and-forget, never blocks
            if result.success and not is_dry_run_success:
                asyncio.create_task(_notify_booking_confirmed(
                    user_id=user_id,
                    reservation_id=reservation_id,
                    venue_name=config.venue_name or f"Venue {config.venue_id}",
                    slot_time=result.slot.date.start if result.slot else "",
                    target_date=config.date.isoformat(),
                    party_size=config.party_size,
                    resy_token=result.resy_token,
                ))

            if is_dry_run_success:
                _set_phase(SnipePhase.DONE, "DRY RUN complete — slot found, booking NOT attempted")
            elif result.success:
                _set_phase(SnipePhase.DONE, "Reservation confirmed!")
            else:
                _set_phase(SnipePhase.DONE, result.error or "Snipe failed")

            result_data = {
                "success": result.success,
                "dry_run": dry_run,
                "booking_path": result.booking_path,
                "attempts": result.attempts,
                "elapsed_seconds": result.elapsed_seconds,
                "error": result.error,
            }
            if result.slot:
                result_data["slot_time"] = result.slot.date.start
                result_data["slot_type"] = result.slot.config.type
            job.broadcast("snipe_result", result_data)

    except asyncio.CancelledError:
        _set_phase(SnipePhase.DONE, "Cancelled")
        await db.update_reservation(reservation_id, status="cancelled", error="Cancelled")
        await _cleanup_claims(reservation_id, "cancelled")
        job.broadcast("snipe_result", {
            "success": False, "error": "Cancelled by user",
            "attempts": job.status.attempt, "elapsed_seconds": 0,
        })
    except Exception as e:
        logger.exception("Snipe %s failed with error", reservation_id)
        safe_msg = _safe_error(e)
        _set_phase(SnipePhase.DONE, f"Error: {safe_msg}")
        await db.update_reservation(reservation_id, status="failed", error=str(e))
        await _cleanup_claims(reservation_id, "failed")
        job.broadcast("snipe_result", {
            "success": False, "error": safe_msg,
            "attempts": job.status.attempt, "elapsed_seconds": 0,
        })
    finally:
        # Release IP assignment back to the pool
        ip_pool.release(snipe_ip_key)
        # Auto-stop scout if this was the last snipe for the venue
        try:
            await _maybe_stop_scout_for_venue(app, config.venue_id)
        except Exception:
            pass  # non-critical
        # Clean up job from manager after a delay (let SSE clients read final state)
        await asyncio.sleep(5)
        app.state.jobs.remove(reservation_id)


# ---------------------------------------------------------------------------
# Booking notifications (SMS / email)
# ---------------------------------------------------------------------------


async def _notify_booking_confirmed(
    *,
    user_id: str,
    reservation_id: str,
    venue_name: str,
    slot_time: str,
    target_date: str,
    party_size: int,
    resy_token: str | None = None,
) -> None:
    """Look up user contact info and send confirmation notification.

    Checks for notification_email on the reservation row first (collected at
    booking time), then falls back to the user's Supabase auth email/phone.
    Runs as a fire-and-forget task — never blocks the booking flow.
    """
    try:
        from tablement.web.notify import get_user_contact, send_booking_confirmation

        # Check reservation row for notification_email (collected at booking time)
        notification_email = None
        try:
            row = await db.get_reservation(reservation_id, user_id)
            if row:
                notification_email = row.get("notification_email")
        except Exception:
            pass

        # Fall back to Supabase auth contact info
        contact = await get_user_contact(user_id)
        email = notification_email or contact.get("email")

        await send_booking_confirmation(
            phone=contact.get("phone"),
            email=email,
            venue_name=venue_name,
            slot_time=slot_time,
            target_date=target_date,
            party_size=party_size,
            resy_token=resy_token,
        )
    except Exception as e:
        logger.warning("Notification failed for user %s: %s (non-critical)", user_id[:8], e)


# ---------------------------------------------------------------------------
# Monitor mode — Layer 2 behavioral separation
# ---------------------------------------------------------------------------


async def _cancellation_snipe_loop(
    app,
    reservation_id: str,
    user_id: str,
    config: SnipeConfig,
    profile: dict,
    job: JobState,
    *,
    group_id: str | None,
    poll_tier: str,
    latest_notify_hours: float,
):
    """Continuous cancellation polling with tiered intervals and rest periods.

    Two tiers:
      Hot (2-5s): reservation is 24-48h away, OR has "earliest available" priority
      Warm (5-15s): everything else during active hours

    Rest periods: after 25-40 min of polling, rest for 1-3 min (hot)
    or 3-5 min (warm). Looks like a real person stepping away.

    Time-of-day schedule (ET):
      9 AM - 10 PM:   active polling
      10 PM - midnight: slow polling (add 3-8s to interval)
      1 AM - 9 AM:    sleep (no polling)

    Layer 2 (Behavioral — Scout/Booking Separation):
    Polling uses scout mode (no auth token) through residential proxy.
    Booking burst uses authenticated client on the same proxy IP.
    From Resy's perspective: anonymous user browsing → logs in → books.
    """
    selector = SlotSelector(window_minutes=config.window_minutes)
    day_str = config.date.isoformat()
    attempt = 0

    # Burst/rest tracking + per-user 429 backoff
    cancel_backoff = 0.0
    burst_start = time_module.monotonic()
    tier_range = CANCEL_TIER_HOT if poll_tier == "hot" else CANCEL_TIER_WARM
    burst_config = CANCEL_BURST_HOT if poll_tier == "hot" else CANCEL_BURST_WARM
    rest_config = CANCEL_REST_HOT if poll_tier == "hot" else CANCEL_REST_WARM
    burst_duration = random.uniform(*burst_config)

    try:
        while True:
            # ---- 1. Check stop conditions ----
            target_date = date.fromisoformat(day_str)

            # Date passed
            if target_date < date.today():
                logger.info("Cancel snipe %s: target date passed", reservation_id)
                await db.update_reservation(reservation_id, status="failed", error="Target date passed")
                await _cleanup_claims(reservation_id, "failed")
                job.broadcast("snipe_result", {"success": False, "error": "Target date passed"})
                break

            # Check if cancelled by group logic or user
            row = await db.get_reservation_by_id(reservation_id)
            if row and row["status"] in ("confirmed", "cancelled", "failed"):
                logger.info("Cancel snipe %s: status=%s, stopping", reservation_id, row["status"])
                break

            # Check latest_notify_hours: stop polling X hours before reservation
            target_dt = datetime.combine(target_date, time(19, 0))  # assume 7pm dinner
            hours_until = (target_dt - datetime.now()).total_seconds() / 3600
            if hours_until < latest_notify_hours:
                logger.info(
                    "Cancel snipe %s: within %.1fh of reservation (limit=%.1fh), stopping",
                    reservation_id, hours_until, latest_notify_hours,
                )
                await db.update_reservation(
                    reservation_id, status="failed",
                    error=f"Stopped {latest_notify_hours}h before reservation (still sold out)",
                )
                job.broadcast("snipe_result", {
                    "success": False,
                    "error": f"No cancellation found within {latest_notify_hours}h of reservation",
                })
                break

            # ---- 2. Check time-of-day (ET) ----
            try:
                from zoneinfo import ZoneInfo
            except ImportError:
                from backports.zoneinfo import ZoneInfo
            et_now = datetime.now(ZoneInfo("America/New_York"))
            et_hour = et_now.hour

            # Sleep hours: 1 AM - 9 AM ET — no polling
            if CANCEL_SLEEP_START <= et_hour < CANCEL_SLEEP_END:
                # Calculate seconds until 9 AM ET
                wake_time = et_now.replace(hour=CANCEL_SLEEP_END, minute=0, second=0, microsecond=0)
                sleep_seconds = (wake_time - et_now).total_seconds()
                if sleep_seconds > 0:
                    job.status.message = f"Sleeping until 9 AM ET ({int(sleep_seconds // 60)}m)"
                    logger.info("Cancel snipe %s: sleeping %dm until 9 AM ET", reservation_id, int(sleep_seconds // 60))
                    await asyncio.sleep(sleep_seconds)
                continue

            # ---- 3. Determine current tier (may change dynamically) ----
            current_tier = _get_cancel_tier(day_str, poll_tier == "hot")
            tier_range = CANCEL_TIER_HOT if current_tier == "hot" else CANCEL_TIER_WARM
            burst_config = CANCEL_BURST_HOT if current_tier == "hot" else CANCEL_BURST_WARM
            rest_config = CANCEL_REST_HOT if current_tier == "hot" else CANCEL_REST_WARM

            # ---- 4. Scout poll (unauthenticated find_slots) ----
            attempt += 1
            try:
                fp = fingerprint_pool.get_fingerprint(user_id)

                async with ResyApiClient(scout=True, user_id=user_id, fingerprint=fp) as scout:
                    slots = await scout.find_slots(
                        config.venue_id, day_str, config.party_size, fast=True,
                    )

                # Update attempt count (non-blocking)
                asyncio.create_task(db.update_reservation(reservation_id, attempts=attempt))

                job.status.attempt = attempt
                job.status.slots_found = len(slots)
                job.status.message = f"Poll {attempt}: {len(slots)} slots ({current_tier})"
                # Broadcast every 10th attempt to avoid SSE flood
                if attempt <= 3 or attempt % 10 == 0 or len(slots) > 0:
                    job.broadcast("snipe_attempt", {
                        "attempt": attempt, "slots_found": len(slots),
                        "message": f"Poll {attempt}: {len(slots)} slots",
                    })

                # ---- 5. If slot found → booking burst ----
                if slots:
                    selected = selector.select(slots, config.time_preferences, config.date)
                    if selected:
                        logger.info("Cancel snipe %s: found matching slot! Booking...", reservation_id)

                        resy_email = profile["resy_email"]
                        resy_password = decrypt_password(profile["resy_password_encrypted"])

                        # Booking burst: same proxy IP + fingerprint as scout
                        async with ResyApiClient(user_id=user_id, fingerprint=fp) as booker:
                            await asyncio.sleep(random.uniform(0.3, 1.0))  # human delay

                            for _auth_try in range(2):
                                try:
                                    auth_resp = await booker.authenticate(resy_email, resy_password)
                                    break
                                except Exception as _auth_err:
                                    if _auth_try < 1:
                                        logger.warning("Cancel snipe %s auth retry: %s", reservation_id, _auth_err)
                                        await asyncio.sleep(1)
                                    else:
                                        raise
                            booker.set_auth_token(auth_resp.token)
                            pm = next(
                                (p for p in auth_resp.payment_methods if p.is_default),
                                auth_resp.payment_methods[0],
                            )
                            booker.prepare_book_template(pm.id)

                            await asyncio.gather(
                                booker.warmup_direct(),
                                asyncio.sleep(random.uniform(0.2, 0.8)),
                            )

                            book_token = await booker.get_details_fast(
                                config_id=selected.config.token,
                                day=day_str,
                                party_size=config.party_size,
                            )

                            await asyncio.sleep(random.uniform(0.1, 0.5))

                            result = await booker.dual_book(
                                book_token=book_token,
                                payment_method_id=pm.id,
                            )

                        # SUCCESS — update DB and cancel group
                        await db.update_reservation(
                            reservation_id,
                            status="confirmed",
                            resy_token=result.resy_token,
                            attempts=attempt,
                        )
                        await _cleanup_claims(reservation_id, "confirmed")

                        job.broadcast("snipe_result", {
                            "success": True,
                            "resy_token": result.resy_token,
                            "booking_path": result.booking_path or None,
                            "slot_time": selected.date.start,
                            "slot_type": selected.config.type,
                            "attempts": attempt,
                            "elapsed_seconds": 0,
                        })

                        # Notify user (SMS / email) — fire-and-forget
                        asyncio.create_task(_notify_booking_confirmed(
                            user_id=user_id,
                            reservation_id=reservation_id,
                            venue_name=config.venue_name or f"Venue {config.venue_id}",
                            slot_time=selected.date.start,
                            target_date=config.date.isoformat(),
                            party_size=config.party_size,
                            resy_token=result.resy_token,
                        ))

                        # Auto-cancel all other reservations in the group
                        if group_id:
                            await _cancel_group_members(app, group_id, reservation_id)

                        # Capture Stripe payment
                        snipe_result = SnipeResult(
                            success=True, resy_token=result.resy_token,
                            booking_path=result.booking_path or None,
                            slot=selected, attempts=attempt, elapsed_seconds=0,
                        )
                        asyncio.create_task(_handle_stripe_result(reservation_id, user_id, snipe_result))
                        break

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 429:
                    cancel_backoff = min(max(cancel_backoff * 2, 1.0), 30.0)
                    logger.warning("Cancel snipe %s: 429 rate limited — backing off %.1fs", reservation_id, cancel_backoff)
                    await asyncio.sleep(cancel_backoff)
                else:
                    logger.warning("Cancel snipe %s poll %d: HTTP %d", reservation_id, attempt, e.response.status_code)
                    cancel_backoff = 0.0
            except Exception as e:
                logger.warning("Cancel snipe %s poll %d failed: %s", reservation_id, attempt, e)
                cancel_backoff = 0.0

            # ---- 6. Check if burst exceeded → rest period ----
            elapsed_burst = time_module.monotonic() - burst_start
            if elapsed_burst > burst_duration:
                rest_seconds = random.uniform(*rest_config)
                job.status.message = f"Resting {int(rest_seconds)}s (stealth pause)"
                logger.info(
                    "Cancel snipe %s: burst %.0fs done, resting %.0fs",
                    reservation_id, elapsed_burst, rest_seconds,
                )
                await asyncio.sleep(rest_seconds)
                burst_start = time_module.monotonic()
                burst_duration = random.uniform(*burst_config)

            # ---- 7. Sleep for tier interval ----
            interval = random.uniform(*tier_range)

            # Late-night slowdown: 10 PM–midnight, add extra seconds
            if CANCEL_ACTIVE_END <= et_hour < 24:
                interval += random.uniform(*CANCEL_SLOWDOWN_EXTRA)

            await asyncio.sleep(interval)

    except asyncio.CancelledError:
        await db.update_reservation(reservation_id, status="cancelled", error="Cancelled")
        await _cleanup_claims(reservation_id, "cancelled")
    finally:
        app.state.jobs.remove(reservation_id)


def _get_cancel_tier(target_date: str, is_priority: bool) -> str:
    """Determine polling tier for a cancellation snipe.

    Hot: 24-48h before reservation, or has "earliest available" priority.
    Warm: everything else.
    """
    if is_priority:
        return "hot"

    try:
        target = date.fromisoformat(target_date)
        hours_until = (datetime.combine(target, time(19, 0)) - datetime.now()).total_seconds() / 3600
        if 24 <= hours_until <= 48:
            return "hot"
    except (ValueError, TypeError):
        pass

    return "warm"


async def _cancel_group_members(app, group_id: str, except_reservation_id: str):
    """Cancel all other reservations in the same group (atomic DB update first).

    Called when ANY reservation in a group confirms. Ensures the user
    NEVER ends up with multiple reservations at the same restaurant.
    Works regardless of the "book earliest" toggle state.
    """
    # Step 1: Atomic DB update — single query cancels all eligible members
    try:
        cancelled_ids = await db.cancel_group_members(group_id, except_reservation_id)
    except Exception as e:
        logger.warning("Failed to atomically cancel group %s: %s", group_id, e)
        return

    # Step 2: Clean up in-memory jobs for cancelled IDs
    for rid in cancelled_ids:
        job = app.state.jobs.get(rid)
        if job:
            job.broadcast("snipe_result", {
                "success": False,
                "error": "Another date in your group was confirmed",
            })
            app.state.jobs.remove(rid)

        # Cancel APScheduler job if any (release snipes)
        scheduler = app.state.scheduler
        if scheduler:
            for prefix in ("snipe_", "monitor_"):
                try:
                    scheduler.remove_job(f"{prefix}{rid}")
                except Exception:
                    pass

    if cancelled_ids:
        logger.info(
            "Group %s: cancelled %d other reservations after %s confirmed",
            group_id, len(cancelled_ids), except_reservation_id,
        )


# ---------------------------------------------------------------------------
# Request coalescing — deduplicate concurrent identical find_slots calls
# ---------------------------------------------------------------------------

_poll_cache: dict[str, tuple[float, list]] = {}  # "venue:date:party" → (timestamp, slots)
_poll_locks: dict[str, asyncio.Lock] = {}


async def _coalesced_find_slots(
    client: ResyApiClient,
    venue_id: int,
    day: str,
    party_size: int,
    *,
    fast: bool = True,
    direct: bool = False,
) -> list:
    """Deduplicate concurrent identical find_slots requests.

    If another snipe polled the same venue+date+party within 30ms, reuse the result.
    Saves N-1 requests when N users snipe the same venue at the same drop time.
    """
    cache_key = f"{venue_id}:{day}:{party_size}"
    now = time_module.monotonic()

    # Check cache first (no lock needed for read)
    if cache_key in _poll_cache:
        ts, cached = _poll_cache[cache_key]
        if now - ts < 0.03:  # 30ms cache window
            return cached

    if cache_key not in _poll_locks:
        _poll_locks[cache_key] = asyncio.Lock()

    async with _poll_locks[cache_key]:
        # Re-check after lock acquisition (another coroutine may have filled cache)
        now = time_module.monotonic()
        if cache_key in _poll_cache:
            ts, cached = _poll_cache[cache_key]
            if now - ts < 0.03:
                return cached

        if direct:
            slots = await client.find_slots_direct(venue_id, day, party_size, fast=fast)
        else:
            slots = await client.find_slots(venue_id, day, party_size, fast=fast)

        _poll_cache[cache_key] = (time_module.monotonic(), slots)
        return slots


# ---------------------------------------------------------------------------
# Snipe loop — aggressive pre-drop polling + parallel slot booking
# ---------------------------------------------------------------------------


async def _snipe_loop(
    client: ResyApiClient,
    config: SnipeConfig,
    selector: SlotSelector,
    payment_id: int,
    dry_run: bool,
    job: JobState,
    *,
    pre_drop_poll: bool = False,
    intel_buffer: SnipeIntelBuffer | None = None,
) -> SnipeResult:
    """Aggressive snipe loop with pre-drop polling and parallel booking.

    Strategy:
    1. PRE-DROP POLLING: Hammer find_slots() with jittered 20-40ms intervals
       starting at T-3s. Most responses return 0 slots. The moment slots appear
       (Resy releases them), we catch them within one poll cycle (~30ms).
    2. PARALLEL DETAILS: When slots are found, fire get_details on the top 2-3
       matching slots simultaneously. First one to return a valid book_token wins.
    3. DUAL-PATH BOOKING: Fire book request via both direct and proxy paths.
       First success wins (Resy is idempotent per user+slot).

    Every poll response is recorded in the intel_buffer (if provided) for
    post-snipe batch insert. This is just a list.append() — zero performance
    impact on the booking hot path.

    The jittered polling intervals (20-40ms random) avoid creating a detectable
    pattern that Imperva/Resy WAF could fingerprint as bot behavior.
    """
    start = time_module.monotonic()
    attempt = 0
    day_str = config.date.isoformat()
    total_interval_ms = 0.0  # Track polling intervals for intel
    drop_detected_at = None  # When slots first appeared
    backoff_seconds = 0.0  # Per-user 429 backoff

    while True:
        attempt += 1
        t0 = time_module.monotonic()
        elapsed = t0 - start

        if elapsed > config.retry.duration_seconds:
            return SnipeResult(
                success=False, attempts=attempt, elapsed_seconds=elapsed,
                error="Retry window exhausted",
            )
        if attempt > config.retry.max_attempts:
            return SnipeResult(
                success=False, attempts=attempt, elapsed_seconds=elapsed,
                error="Max attempts reached",
            )

        try:
            # Step 1: Find slots — coalesced to deduplicate when multiple snipes
            # target the same venue. Uses direct client (19ms) for pre-drop polling.
            slots = await _coalesced_find_slots(
                client, config.venue_id, day_str, config.party_size,
                fast=True, direct=pre_drop_poll,
            )

            # RECORD EVERY POLL — this is just a list.append(), ~0ms overhead
            if intel_buffer is not None:
                intel_buffer.record(len(slots), slots if slots else None)

            job.status.attempt = attempt
            job.status.slots_found = len(slots)
            # Only broadcast every 10th attempt during pre-drop to avoid SSE flood
            if attempt <= 3 or attempt % 10 == 0 or len(slots) > 0:
                job.broadcast("snipe_attempt", {
                    "attempt": attempt, "slots_found": len(slots),
                    "message": f"Attempt {attempt}: {len(slots)} slots",
                })

            if not slots:
                # Jittered sleep: 20-40ms random interval (avg ~30ms = ~33 req/s)
                if pre_drop_poll:
                    jitter_ms = random.uniform(PRE_DROP_POLL_MIN_MS, PRE_DROP_POLL_MAX_MS)
                    await asyncio.sleep(jitter_ms / 1000)
                elif config.retry.interval_seconds > 0:
                    await asyncio.sleep(config.retry.interval_seconds)
                total_interval_ms += (time_module.monotonic() - t0) * 1000
                continue

            # SLOTS FOUND — record drop detection time for intel
            if intel_buffer is not None:
                intel_buffer.mark_drop()
            try:
                from zoneinfo import ZoneInfo
            except ImportError:
                from backports.zoneinfo import ZoneInfo
            drop_detected_at = datetime.now(ZoneInfo(config.drop_time.timezone if config.drop_time else "America/New_York"))

            job.broadcast("snipe_attempt", {
                "attempt": attempt, "slots_found": len(slots),
                "message": f"SLOTS LIVE! {len(slots)} slots found on attempt {attempt}",
            })

            # Step 2: Select top matching slots for parallel booking
            top_matches = selector.select_top(
                slots, config.time_preferences, config.date,
                limit=PARALLEL_DETAILS_SLOTS,
            )
            if not top_matches:
                continue

            # Helper: stamp intel fields onto result
            def _stamp_intel(result: SnipeResult) -> SnipeResult:
                slot_types = list({s.config.type for s in slots if s.config.type})
                slot_times = sorted(s.date.start for s in slots)
                result._drop_detected_at = drop_detected_at
                result._slots_at_drop = len(slots)
                result._slot_types_at_drop = slot_types
                result._first_slot_time = slot_times[0] if slot_times else None
                result._last_slot_time = slot_times[-1] if slot_times else None
                result._drop_poll_attempt = attempt
                result._avg_poll_interval_ms = round(total_interval_ms / max(attempt - 1, 1), 1)
                return result

            if dry_run:
                return _stamp_intel(SnipeResult(
                    success=True, slot=top_matches[0], attempts=attempt,
                    elapsed_seconds=time_module.monotonic() - start,
                    error="DRY RUN - booking skipped",
                ))

            # Step 3: PARALLEL get_details on top matches
            # Fire details on all top matches simultaneously. First valid
            # book_token wins. This looks like a user clicking through
            # different time options (normal behavior).
            detail_tasks = [
                asyncio.create_task(
                    client.get_details_fast(
                        config_id=slot.config.token,
                        day=day_str,
                        party_size=config.party_size,
                    )
                )
                for slot in top_matches
            ]

            # Wait for all, use first valid token
            detail_results = await asyncio.gather(*detail_tasks, return_exceptions=True)

            booked = False
            for i, token_or_err in enumerate(detail_results):
                if isinstance(token_or_err, Exception) or not token_or_err:
                    continue

                book_token = token_or_err
                booked_slot = top_matches[i]

                try:
                    # Step 4: Race booking via direct EC2 + proxy simultaneously.
                    # book_token is single-use — second request fails harmlessly.
                    # Cancellation snipe already uses dual_book (line 1442).
                    result = await client.dual_book(
                        book_token=book_token,
                        payment_method_id=payment_id,
                    )

                    return _stamp_intel(SnipeResult(
                        success=True,
                        resy_token=result.resy_token,
                        slot=booked_slot,
                        attempts=attempt,
                        elapsed_seconds=time_module.monotonic() - start,
                    ))
                except Exception as book_err:
                    logger.warning(
                        "Book failed for slot %s: %s, trying next...",
                        booked_slot.date.start, book_err,
                    )
                    continue

            # All detail/book attempts failed — retry from find_slots
            logger.warning("All %d parallel book attempts failed, retrying...", len(top_matches))

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                backoff_seconds = min(max(backoff_seconds * 2, 1.0), 30.0)
                logger.warning("Attempt %d: 429 rate limited — backing off %.1fs", attempt, backoff_seconds)
                await asyncio.sleep(backoff_seconds)
            else:
                logger.warning("Attempt %d: HTTP %d", attempt, e.response.status_code)
                if pre_drop_poll:
                    await asyncio.sleep(random.uniform(PRE_DROP_POLL_MIN_MS, PRE_DROP_POLL_MAX_MS) / 1000)
            continue
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
            logger.warning("Attempt %d: connection error: %s — reconnecting client", attempt, e)
            try:
                await client.aclose()
            except Exception:
                pass
            # Recreate client with same auth state
            new_client = ResyApiClient()
            await new_client.__aenter__()
            if hasattr(client, '_auth_token') and client._auth_token:
                new_client.set_auth_token(client._auth_token)
            if hasattr(client, '_book_template'):
                new_client._book_template = client._book_template
            new_client.set_snipe_mode(True)
            client = new_client
            continue
        except Exception as e:
            logger.warning("Attempt %d failed: %s", attempt, e)
            if pre_drop_poll:
                await asyncio.sleep(random.uniform(PRE_DROP_POLL_MIN_MS, PRE_DROP_POLL_MAX_MS) / 1000)
            continue


# ---------------------------------------------------------------------------
# Post-snipe velocity tracking (non-blocking, runs in background)
# ---------------------------------------------------------------------------

# Post-drop velocity: aggressive early snapshots, stop quickly when slots gone
# Everything past 10s is useless for competitive restaurants — slots vanish in ms
VELOCITY_SNAPSHOT_OFFSETS_S = [0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0]


async def _post_snipe_velocity_tracking(
    client: ResyApiClient,
    venue_id: int,
    target_date: str,
    party_size: int,
    reservation_id: str | None = None,
) -> None:
    """Take slot snapshots after booking to measure how fast slots disappear.

    Runs as a background task — does NOT block or slow down the booking.
    Uses the existing HTTP connection from the snipe client.

    Early termination: stops immediately after 2 consecutive polls return 0 slots.
    """
    try:
        prev_offset = 0.0
        consecutive_empty = 0

        for offset_s in VELOCITY_SNAPSHOT_OFFSETS_S:
            sleep_time = offset_s - prev_offset
            prev_offset = offset_s
            await asyncio.sleep(sleep_time)

            try:
                slots = await client.find_slots(
                    venue_id, target_date, party_size, fast=True,
                )

                snapshot = {
                    "venue_id": venue_id,
                    "target_date": target_date,
                    "slot_count": len(slots),
                    "slots_json": [
                        {"time": s.date.start, "type": s.config.type}
                        for s in slots
                    ],
                    "ms_since_drop": offset_s * 1000,
                    "source": "post_drop",
                }
                if reservation_id:
                    snapshot["reservation_id"] = reservation_id

                await db.insert_slot_snapshot(snapshot)
                logger.debug(
                    "Post-snipe velocity %s +%.2fs: %d slots",
                    venue_id, offset_s, len(slots),
                )

                # Early termination: 2 consecutive empty polls = all gone
                if not slots:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        logger.debug(
                            "All slots gone (2 consecutive empty at +%.2fs), stopping velocity",
                            offset_s,
                        )
                        break
                else:
                    consecutive_empty = 0

            except Exception:
                continue
    except Exception as e:
        logger.warning("Post-snipe velocity tracking failed: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _busy_wait(seconds: float):
    """Busy-wait for sub-ms precision. Runs in a thread."""
    target = time_module.monotonic() + seconds
    while time_module.monotonic() < target:
        pass


def _build_snipe_config(body: ReservationCreateRequest) -> SnipeConfig:
    """Build a SnipeConfig from the API request."""
    return SnipeConfig(
        venue_id=body.venue_id,
        venue_name=body.venue_name,
        party_size=body.party_size,
        date=date.fromisoformat(body.date),
        time_preferences=[
            TimePreference(
                time=time.fromisoformat(tp.time),
                seating_type=tp.seating_type if tp.seating_type else None,
            )
            for tp in body.time_preferences
        ],
        drop_time=DropTime(
            hour=body.drop_time.hour,
            minute=body.drop_time.minute,
            second=body.drop_time.second,
            timezone=body.drop_time.timezone,
            days_ahead=body.drop_time.days_ahead,
        )
        if body.drop_time
        else DropTime(hour=0, minute=0, timezone="America/New_York", days_ahead=30),
    )


def _row_to_out(row: dict) -> ReservationOut:
    """Convert a DB row to the API response model."""
    return ReservationOut(
        id=row["id"],
        venue_id=row["venue_id"],
        venue_name=row["venue_name"],
        party_size=row["party_size"],
        target_date=str(row["target_date"]),
        mode=row["mode"],
        status=row["status"],
        time_preferences=row.get("time_preferences"),
        drop_time_config=row.get("drop_time_config"),
        attempts=row.get("attempts", 0),
        elapsed_seconds=row.get("elapsed_seconds"),
        error=row.get("error"),
        created_at=str(row.get("created_at", "")),
        group_id=row.get("group_id"),
        poll_tier=row.get("poll_tier"),
        latest_notify_hours=row.get("latest_notify_hours"),
    )


async def _handle_stripe_result(reservation_id: str, user_id: str, result: SnipeResult):
    """Charge the user on successful booking, log on failure."""
    if not result.success:
        return

    try:
        from tablement.web.routes.payments import charge_for_booking
        charge_result = await charge_for_booking(user_id, reservation_id)
        logger.info(
            "Stripe result for %s: %s",
            reservation_id, charge_result,
        )
    except Exception as e:
        # Never let billing errors affect the booking outcome
        logger.error("Billing error for reservation %s: %s", reservation_id, e)


# ---------------------------------------------------------------------------
# Job Recovery — re-schedule in-flight jobs after crash/restart
# ---------------------------------------------------------------------------


def _build_snipe_config_from_row(row: dict) -> SnipeConfig:
    """Rebuild a SnipeConfig from a DB reservation row."""
    time_prefs = row.get("time_preferences") or []
    drop_config = row.get("drop_time_config") or {}

    return SnipeConfig(
        venue_id=row["venue_id"],
        venue_name=row.get("venue_name", ""),
        party_size=row.get("party_size", 2),
        date=date.fromisoformat(str(row["target_date"])),
        time_preferences=[
            TimePreference(
                time=time.fromisoformat(tp["time"]),
                seating_type=tp.get("seating_type"),
            )
            for tp in time_prefs
            if tp.get("time")
        ],
        drop_time=DropTime(
            hour=drop_config.get("hour", 0),
            minute=drop_config.get("minute", 0),
            second=drop_config.get("second", 0),
            timezone=drop_config.get("timezone", "America/New_York"),
            days_ahead=drop_config.get("days_ahead", 30),
        )
        if drop_config
        else DropTime(hour=0, minute=0, timezone="America/New_York", days_ahead=30),
    )


async def recover_jobs(app) -> int:
    """Recover in-flight reservations after server crash/restart.

    Called during app startup. For each recoverable reservation:
    - mode=snipe + status=sniping → mark failed (was mid-execution, state lost)
    - mode=snipe + drop_time passed >5min → mark failed
    - mode=snipe + drop_time future → reschedule
    - mode=monitor → restart cancellation monitoring
    """
    try:
        rows = await db.get_recoverable_reservations()
    except Exception as e:
        logger.warning("Job recovery failed to query DB: %s", e)
        return 0

    if not rows:
        return 0

    recovered = 0
    failed = 0

    for row in rows:
        rid = row["id"]
        user_id = row["user_id"]
        mode = row.get("mode", "snipe")
        status = row.get("status", "")

        # Load user profile (need Resy credentials)
        try:
            profile = await db.get_profile(user_id)
        except Exception:
            profile = None

        if not profile or not profile.get("resy_email"):
            await db.update_reservation(rid, status="failed", error="Recovery failed: no Resy credentials")
            failed += 1
            continue

        if mode == "snipe":
            # Was mid-execution → state is lost
            if status == "sniping":
                await db.update_reservation(rid, status="failed", error="Server restarted during snipe")
                failed += 1
                continue

            # Check if drop time has passed
            config = _build_snipe_config_from_row(row)
            scheduler = PrecisionScheduler()
            try:
                drop_dt = scheduler.calculate_drop_datetime(config)
            except Exception:
                await db.update_reservation(rid, status="failed", error="Recovery failed: invalid drop time")
                failed += 1
                continue

            now = datetime.now(drop_dt.tzinfo)
            if (now - drop_dt).total_seconds() > 300:  # >5 min past drop
                await db.update_reservation(rid, status="failed", error="Drop time passed during restart")
                failed += 1
                continue

            # Reschedule — build a mock request body
            body = _mock_create_request_from_row(row)
            _schedule_snipe(app, rid, user_id, body, profile, dry_run=False)
            recovered += 1

        elif mode == "monitor":
            # Restart cancellation monitoring
            body = _mock_create_request_from_row(row)
            group_id = row.get("group_id")
            poll_tier = row.get("poll_tier", "warm")
            latest_notify_hours = row.get("latest_notify_hours", 3.0)

            _schedule_cancellation_snipe(
                app, rid, user_id, body, profile,
                group_id=group_id,
                poll_tier=poll_tier,
                latest_notify_hours=latest_notify_hours,
            )
            recovered += 1

    logger.info("Job recovery: %d recovered, %d failed (of %d total)", recovered, failed, len(rows))
    return recovered


def _mock_create_request_from_row(row: dict) -> ReservationCreateRequest:
    """Build a ReservationCreateRequest from a DB row for recovery."""
    from tablement.web.schemas import DropTimeIn, TimePreferenceIn

    time_prefs = row.get("time_preferences") or []
    drop_config = row.get("drop_time_config") or {}

    return ReservationCreateRequest(
        venue_id=row["venue_id"],
        venue_name=row.get("venue_name", ""),
        party_size=row.get("party_size", 2),
        date=str(row["target_date"]),
        mode=row.get("mode", "snipe"),
        time_preferences=[
            TimePreferenceIn(
                time=tp.get("time", "19:00"),
                seating_type=tp.get("seating_type"),
            )
            for tp in time_prefs
        ] if time_prefs else [TimePreferenceIn(time="19:00")],
        drop_time=DropTimeIn(
            hour=drop_config.get("hour", 0),
            minute=drop_config.get("minute", 0),
            second=drop_config.get("second", 0),
            timezone=drop_config.get("timezone", "America/New_York"),
            days_ahead=drop_config.get("days_ahead", 30),
        ) if drop_config else None,
    )
