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
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from tablement.api import ResyApiClient
from tablement.fingerprint import fingerprint_pool
from tablement.models import DropTime, SnipeConfig, SnipeResult, TimePreference
from tablement.scheduler import PrecisionScheduler
from tablement.selector import SlotSelector
from tablement.web import db
from tablement.web.deps import get_user_id
from tablement.web.encryption import decrypt_password
from tablement.web.schemas import (
    ReservationCreateRequest,
    ReservationListResponse,
    ReservationOut,
    SnipeResultOut,
)
from tablement.web.state import JobState, SnipePhase

logger = logging.getLogger(__name__)
router = APIRouter()

# Pre-drop polling: start hammering find_slots() this many seconds BEFORE drop
PRE_DROP_POLL_SECONDS = 2.0
# Polling interval range (jittered to avoid detection)
PRE_DROP_POLL_MIN_MS = 20   # ~50 req/s at fastest
PRE_DROP_POLL_MAX_MS = 40   # ~25 req/s at slowest, avg ~33 req/s

# Legacy pre-fire offset (still used for busy-wait precision entry)
PRE_FIRE_MS = 200

# Warmup timing (Phase 6: earlier warmup)
WARMUP_BEFORE_SECONDS = 30  # Was 5s — earlier warmup for better TCP window
PING_INTERVAL_SECONDS = 10  # Was 15s — more frequent keep-alive

# Parallel booking: get_details on top N matching slots simultaneously
PARALLEL_DETAILS_SLOTS = 3

# Monitor polling interval: 30s base ± JITTER_SECONDS
MONITOR_INTERVAL_BASE = 30
MONITOR_JITTER_SECONDS = 5


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
# CRUD
# ---------------------------------------------------------------------------


@router.post("/", response_model=ReservationOut)
async def create_reservation(
    body: ReservationCreateRequest,
    request: Request,
    user_id: str = Depends(get_user_id),
):
    """Create a new reservation job (snipe or monitor)."""
    # Validate: snipe mode requires drop_time
    if body.mode == "snipe" and not body.drop_time:
        raise HTTPException(400, "Snipe mode requires drop_time configuration")

    # Check user has linked Resy account
    profile = await db.get_profile(user_id)
    if not profile or not profile.get("resy_email"):
        raise HTTPException(400, "Please link your Resy account first (POST /api/auth/link-resy)")

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

            while True:
                msg = await queue.get()
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
    """Schedule a monitor job that polls with jitter around 30 seconds.

    Layer 2 (Behavioral): Uses jittered intervals instead of exact 30s to
    avoid creating a detectable pattern in request timing.
    """
    scheduler = app.state.scheduler

    config = _build_snipe_config(body)
    job = app.state.jobs.create(reservation_id, user_id)

    if scheduler:
        # Use base interval — jitter is added inside _monitor_poll via sleep
        scheduler.add_job(
            _monitor_poll,
            trigger="interval",
            seconds=MONITOR_INTERVAL_BASE,
            id=f"monitor_{reservation_id}",
            replace_existing=True,
            args=[app, reservation_id, user_id, config, profile],
            jitter=MONITOR_JITTER_SECONDS,  # APScheduler built-in jitter
        )
        job.status.phase = SnipePhase.MONITORING
        job.status.message = "Monitoring for cancellations (every ~30s)"
        logger.info("Monitor %s started (~%ds interval)", reservation_id, MONITOR_INTERVAL_BASE)
    else:
        # Fallback: asyncio loop
        job.task = asyncio.create_task(
            _monitor_loop(app, reservation_id, user_id, config, profile, job)
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

        # Single persistent connection with per-user fingerprint + proxy
        async with ResyApiClient(user_id=user_id) as client:
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

            # Authenticate at T-60
            _set_phase(SnipePhase.AUTHENTICATING, "Authenticating with Resy...")
            auth_resp = await client.authenticate(resy_email, resy_password)
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

            # Apply Resy clock offset
            resy_offset = warmup_results[1] if isinstance(warmup_results[1], float) else 0.0
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

            # AGGRESSIVE PRE-DROP POLLING
            # Start hammering find_slots() before drop. Jittered intervals
            # (20-40ms) to avoid pattern detection. Slots will return empty
            # until the exact moment Resy releases them.
            _set_phase(SnipePhase.SNIPING, f"Pre-drop polling (T-{PRE_DROP_POLL_SECONDS}s)...")
            await db.update_reservation(reservation_id, status="sniping")

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

            # Stripe: capture on success, release on failure (skip for dry run)
            if not is_dry_run_success:
                _handle_stripe_result(reservation_id, result)

            if is_dry_run_success:
                _set_phase(SnipePhase.DONE, "DRY RUN complete — slot found, booking NOT attempted")
            elif result.success:
                _set_phase(SnipePhase.DONE, "Reservation confirmed!")
            else:
                _set_phase(SnipePhase.DONE, result.error or "Snipe failed")

            result_data = {
                "success": result.success,
                "dry_run": dry_run,
                "resy_token": result.resy_token,
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
        job.broadcast("snipe_result", {
            "success": False, "error": "Cancelled by user",
            "attempts": job.status.attempt, "elapsed_seconds": 0,
        })
    except Exception as e:
        logger.exception("Snipe %s failed with error", reservation_id)
        _set_phase(SnipePhase.DONE, f"Error: {e}")
        await db.update_reservation(reservation_id, status="failed", error=str(e))
        job.broadcast("snipe_result", {
            "success": False, "error": str(e),
            "attempts": job.status.attempt, "elapsed_seconds": 0,
        })
    finally:
        # Clean up job from manager after a delay (let SSE clients read final state)
        await asyncio.sleep(5)
        app.state.jobs.remove(reservation_id)


# ---------------------------------------------------------------------------
# Monitor mode — Layer 2 behavioral separation
# ---------------------------------------------------------------------------


async def _monitor_poll(
    app, reservation_id: str, user_id: str, config: SnipeConfig, profile: dict,
):
    """Single poll iteration for monitor mode. Called by APScheduler ~every 30s.

    Layer 2 (Behavioral — Scout/Booking Separation):
    The monitoring phase uses a "scout" client (no auth token) for find_slots().
    This means Resy sees unauthenticated API-key-only requests checking availability,
    which is exactly what their website does for anonymous users browsing venues.

    When a matching slot is found, we create a SEPARATE authenticated client
    for the 3-call booking burst (authenticate → get_details → book). From
    Resy's perspective, the user's account activity looks like:
    1. User opens the page (auth)
    2. User gets slot details (get_details)
    3. User books (book)
    — perfectly normal behavior, not a bot.
    """
    selector = SlotSelector(window_minutes=config.window_minutes)
    day_str = config.date.isoformat()
    job = app.state.jobs.get(reservation_id)

    # Check if target date has passed
    if date.fromisoformat(day_str) < date.today():
        logger.info("Monitor %s: target date passed, cancelling", reservation_id)
        await db.update_reservation(reservation_id, status="failed", error="Target date passed")
        if job:
            job.broadcast("snipe_result", {"success": False, "error": "Target date passed"})
            app.state.jobs.remove(reservation_id)
        scheduler = app.state.scheduler
        if scheduler:
            try:
                scheduler.remove_job(f"monitor_{reservation_id}")
            except Exception:
                pass
        return

    try:
        resy_email = profile["resy_email"]
        resy_password = decrypt_password(profile["resy_password_encrypted"])

        # Assign a consistent fingerprint for this user across scout + booking
        fp = fingerprint_pool.get_fingerprint(user_id)

        # ---- SCOUT PHASE: unauthenticated find_slots() ----
        # Uses scout=True → no auth token in headers → looks like anonymous browsing
        async with ResyApiClient(scout=True, user_id=user_id, fingerprint=fp) as scout:
            slots = await scout.find_slots(
                config.venue_id, day_str, config.party_size, fast=True,
            )

        # Update attempt count
        attempt = (await db.get_reservation(reservation_id, user_id) or {}).get("attempts", 0) + 1
        await db.update_reservation(reservation_id, attempts=attempt)

        if job:
            job.status.attempt = attempt
            job.status.slots_found = len(slots)
            job.broadcast("snipe_attempt", {
                "attempt": attempt, "slots_found": len(slots),
                "message": f"Poll {attempt}: {len(slots)} slots",
            })

        if not slots:
            return

        # Select best slot
        selected = selector.select(
            slots, config.time_preferences, config.date,
        )
        if not selected:
            return

        logger.info("Monitor %s: found matching slot! Booking...", reservation_id)

        # ---- BOOKING PHASE: authenticated burst (auth → details → book) ----
        # Layer 2: Fresh authenticated client, same fingerprint + proxy as scout.
        # From Resy's view: user opens page → picks slot → books. Normal behavior.
        async with ResyApiClient(user_id=user_id, fingerprint=fp) as booker:
            # Small realistic delay (human would take 0.5-2s to click "book")
            await asyncio.sleep(random.uniform(0.3, 1.0))

            auth_resp = await booker.authenticate(resy_email, resy_password)
            booker.set_auth_token(auth_resp.token)
            pm = next(
                (p for p in auth_resp.payment_methods if p.is_default),
                auth_resp.payment_methods[0],
            )

            # Pre-build book template for speed
            booker.prepare_book_template(pm.id)

            # Warm direct client in parallel with human delay
            await asyncio.gather(
                booker.warmup_direct(),
                asyncio.sleep(random.uniform(0.2, 0.8)),
            )

            # Fast book_token extraction
            book_token = await booker.get_details_fast(
                config_id=selected.config.token,
                day=day_str,
                party_size=config.party_size,
            )

            await asyncio.sleep(random.uniform(0.1, 0.5))

            # Dual-path booking (direct + proxy race)
            result = await booker.dual_book(
                book_token=book_token,
                payment_method_id=pm.id,
            )

        # Success!
        await db.update_reservation(
            reservation_id,
            status="confirmed",
            resy_token=result.resy_token,
            attempts=attempt,
        )

        if job:
            job.broadcast("snipe_result", {
                "success": True,
                "resy_token": result.resy_token,
                "slot_time": selected.date.start,
                "slot_type": selected.config.type,
                "attempts": attempt,
                "elapsed_seconds": 0,
            })
            app.state.jobs.remove(reservation_id)

        # Remove the interval job
        scheduler = app.state.scheduler
        if scheduler:
            try:
                scheduler.remove_job(f"monitor_{reservation_id}")
            except Exception:
                pass

        # Capture Stripe payment
        snipe_result = SnipeResult(
            success=True, resy_token=result.resy_token,
            slot=selected, attempts=attempt, elapsed_seconds=0,
        )
        _handle_stripe_result(reservation_id, snipe_result)

    except Exception as e:
        logger.warning("Monitor %s poll failed: %s", reservation_id, e)


def _get_poll_interval(target_date: str, now: datetime | None = None) -> float:
    """Variable polling frequency based on cancellation probability.

    Polls faster during peak cancellation windows:
    - 24-48h before reservation: highest cancellation rate
    - 2-6h before (day-of morning): day-of cancellations
    - Lunch/dinner decision hours (11-12, 17-18)
    """
    if now is None:
        now = datetime.now()
    try:
        target = datetime.combine(date.fromisoformat(target_date), time(0, 0))
    except (ValueError, TypeError):
        return MONITOR_INTERVAL_BASE

    hours_until = (target - now).total_seconds() / 3600

    # Peak cancellation windows — poll at 15s instead of 30s
    if 24 <= hours_until <= 48:
        return MONITOR_INTERVAL_BASE * 0.5  # 15s
    if 2 <= hours_until <= 6:
        return MONITOR_INTERVAL_BASE * 0.5  # 15s
    # Lunch/dinner decision time — poll at ~21s
    if now.hour in (11, 12, 17, 18):
        return MONITOR_INTERVAL_BASE * 0.7  # 21s

    return MONITOR_INTERVAL_BASE


async def _monitor_loop(
    app, reservation_id: str, user_id: str, config: SnipeConfig, profile: dict, job: JobState,
):
    """Fallback monitor loop using asyncio.sleep (no APScheduler).

    Layer 2 (Behavioral): Uses jittered sleep intervals to avoid creating
    a detectable polling pattern. Variable frequency based on cancellation
    probability (faster during peak cancellation windows).
    """
    job.status.phase = SnipePhase.MONITORING
    job.status.message = "Monitoring for cancellations..."
    day_str = config.date.isoformat()

    try:
        while True:
            await _monitor_poll(app, reservation_id, user_id, config, profile)
            # Check if job was completed
            row = await db.get_reservation(reservation_id, user_id)
            if row and row["status"] in ("confirmed", "failed", "cancelled"):
                break
            # Variable interval based on cancellation probability + jitter
            base = _get_poll_interval(day_str)
            jitter = random.uniform(-MONITOR_JITTER_SECONDS, MONITOR_JITTER_SECONDS)
            await asyncio.sleep(base + jitter)
    except asyncio.CancelledError:
        await db.update_reservation(reservation_id, status="cancelled", error="Cancelled")
    finally:
        app.state.jobs.remove(reservation_id)


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
            # Step 1: Find slots (single fast call in tight loop — faster than
            # overlapping volleys for pre-drop polling since we're already
            # hammering at 30ms intervals)
            slots = await client.find_slots(
                config.venue_id, day_str, config.party_size, fast=True,
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
                    # Step 4: Single-path booking (direct from EC2 = fastest)
                    # Dual-path (proxy + direct simultaneously) is suspicious —
                    # two IPs booking the same slot at the same instant.
                    # Direct from EC2 in us-east-1 is already 5-15ms RTT.
                    result = await client.book(
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

        except Exception as e:
            logger.warning("Attempt %d failed: %s", attempt, e)
            # Jittered retry even on error
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
        resy_token=row.get("resy_token"),
        attempts=row.get("attempts", 0),
        elapsed_seconds=row.get("elapsed_seconds"),
        error=row.get("error"),
        created_at=str(row.get("created_at", "")),
    )


def _handle_stripe_result(reservation_id: str, result: SnipeResult):
    """Capture Stripe payment on success, release on failure."""
    try:
        import stripe

        stripe_key = os.environ.get("STRIPE_SECRET_KEY", "")
        if not stripe_key:
            return

        stripe.api_key = stripe_key

        # We'd need to look up the payment_intent_id from DB
        # For now this is a placeholder — will be connected in Phase 4
        # when Stripe is fully integrated
    except ImportError:
        pass
