"""OpenTable reservation execution — separate from Resy execution.

Contains OT-specific snipe and cancellation monitor logic.
Mirrors the Resy functions in reservations.py but uses:
  - OTReservationSniper (3-step: find → lock → book)
  - OpenTableApiClient (mobile-api.opentable.com)
  - Bearer token auth (not email/password)
  - 30s minimum poll interval for cancellation monitoring

Called by reservations.py via platform dispatch — no shared execution logic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time as time_module
from datetime import date, datetime, time, timedelta

from tablement.models import DropTime, SnipeConfig, SnipeResult, TimePreference
from tablement.opentable.api import OpenTableApiClient
from tablement.opentable.models import OTAuthToken
from tablement.opentable.sniper import OTReservationSniper
from tablement.selector import SlotSelector
from tablement.scheduler import PrecisionScheduler
from tablement.web import db
from tablement.web.encryption import decrypt_password
from tablement.web.ip_pool import ip_pool
from tablement.web.state import JobState, SnipePhase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# OT Snipe timing constants (looser than Resy — OT is stricter on polling)
# ---------------------------------------------------------------------------

# Auth timing: OT token is pre-validated, so we only need warmup
OT_WARMUP_BEFORE_MIN_SECONDS = 20
OT_WARMUP_BEFORE_MAX_SECONDS = 40

# Snipe loop: tighter retry window (OT locks are time-sensitive)
OT_SNIPE_DURATION_SECONDS = 15.0
OT_SNIPE_MAX_ATTEMPTS = 100
OT_SNIPE_POLL_MIN_MS = 50    # slower than Resy (OT is stricter)
OT_SNIPE_POLL_MAX_MS = 100

# ---------------------------------------------------------------------------
# OT Cancellation sniping — 30s minimum poll interval (OT rate limits)
# ---------------------------------------------------------------------------

# Two tiers (slower than Resy because OT enforces 30s minimum)
OT_CANCEL_TIER_HOT = (30, 45)       # seconds — 24-48h before, or priority
OT_CANCEL_TIER_WARM = (45, 90)      # seconds — normal active polling

# Rest periods
OT_CANCEL_REST_HOT = (120, 300)     # 2-5 min rest after hot burst
OT_CANCEL_REST_WARM = (300, 600)    # 5-10 min rest after warm burst
OT_CANCEL_BURST_HOT = (20 * 60, 35 * 60)    # 20-35 min burst before rest
OT_CANCEL_BURST_WARM = (15 * 60, 25 * 60)   # 15-25 min burst before rest

# Active hours (ET) — same as Resy
OT_CANCEL_ACTIVE_START = 9
OT_CANCEL_ACTIVE_END = 22
OT_CANCEL_SLEEP_START = 1
OT_CANCEL_SLEEP_END = 9

# Late-night slowdown
OT_CANCEL_SLOWDOWN_EXTRA = (10, 30)   # add 10-30s (more conservative than Resy)


# ---------------------------------------------------------------------------
# Helper: build OTAuthToken from profile
# ---------------------------------------------------------------------------


def _build_ot_auth_token(profile: dict) -> OTAuthToken:
    """Decrypt OT bearer token from profile and build OTAuthToken."""
    encrypted = profile.get("opentable_bearer_token_encrypted", "")
    if not encrypted:
        raise ValueError("No OpenTable bearer token found in profile")

    bearer_token = decrypt_password(encrypted)

    return OTAuthToken(
        bearer_token=bearer_token,
        diner_id=profile.get("opentable_diner_id", ""),
        gpid=profile.get("opentable_gpid", ""),
        phone=profile.get("opentable_phone", ""),
    )


# ---------------------------------------------------------------------------
# OT Snipe execution (drop sniping — runs at scheduled drop time)
# ---------------------------------------------------------------------------


async def _run_ot_snipe(
    app,
    reservation_id: str,
    user_id: str,
    config: SnipeConfig,
    profile: dict,
    dry_run: bool,
    job: JobState,
):
    """Execute the full OpenTable snipe flow for a reservation.

    Simpler than Resy: OT bearer token is pre-validated (no email/password
    auth at T-60s). The OTReservationSniper handles:
      - Token validation
      - Akamai warmup + clock calibration
      - 3-step snipe loop (find → lock → book)
    """
    scheduler = PrecisionScheduler()
    drop_dt = scheduler.calculate_drop_datetime(config)
    day_str = config.date.isoformat()

    def _set_phase(phase: SnipePhase, message: str, **kw):
        job.status.phase = phase
        job.status.message = message
        for k, v in kw.items():
            setattr(job.status, k, v)
        job.broadcast("snipe_phase", {"phase": phase.value, "message": message, **kw})

    # Assign a dedicated IP for this snipe
    snipe_ip_key = f"ot-snipe-{user_id}-{reservation_id}"
    local_ip = ip_pool.get_ip(snipe_ip_key)
    logger.info("OT snipe %s: bound to IP %s", reservation_id, local_ip)

    try:
        # NTP offset check
        ntp_task = asyncio.create_task(scheduler.check_ntp_offset_async())

        _set_phase(SnipePhase.WAITING, f"Waiting until {drop_dt.strftime('%H:%M:%S %Z')}")
        await db.update_reservation(reservation_id, status="scheduled")

        # Build OT auth token from profile
        try:
            auth_token = _build_ot_auth_token(profile)
        except ValueError as exc:
            _set_phase(SnipePhase.DONE, f"Error: {exc}")
            await db.update_reservation(reservation_id, status="failed", error=str(exc))
            job.broadcast("snipe_result", {
                "success": False, "error": str(exc), "attempts": 0, "elapsed_seconds": 0,
            })
            return

        # Collect NTP result
        ntp_offset = await ntp_task
        if ntp_offset is not None:
            logger.info("NTP offset: %.1fms", ntp_offset * 1000)
        drop_dt = scheduler.compensate_drop_time(drop_dt)

        # Wait until warmup window (T-20s to T-40s before drop)
        warmup_before = random.uniform(OT_WARMUP_BEFORE_MIN_SECONDS, OT_WARMUP_BEFORE_MAX_SECONDS)
        warmup_time = drop_dt - timedelta(seconds=warmup_before)

        now = datetime.now(drop_dt.tzinfo)
        wait_secs = (warmup_time - now).total_seconds()

        while wait_secs > 0:
            remaining = (drop_dt - datetime.now(drop_dt.tzinfo)).total_seconds()
            job.broadcast("countdown_tick", {
                "seconds_remaining": remaining,
                "phase": "waiting",
            })
            sleep_time = min(wait_secs, 1.0)
            await asyncio.sleep(sleep_time)
            wait_secs = (warmup_time - datetime.now(drop_dt.tzinfo)).total_seconds()

        # Warmup phase: the OTReservationSniper.execute() handles warmup internally
        # We just need to wait until drop time, then launch the sniper
        _set_phase(SnipePhase.WARMING, "Warming OT connections + calibrating clock...")

        # Wait until near drop time (T-2s)
        poll_entry = drop_dt - timedelta(seconds=2.0)
        while True:
            now = datetime.now(drop_dt.tzinfo)
            remaining = (drop_dt - now).total_seconds()
            if (poll_entry - now).total_seconds() <= 0:
                break
            job.broadcast("countdown_tick", {
                "seconds_remaining": remaining,
                "phase": "warming",
            })
            await asyncio.sleep(min((poll_entry - now).total_seconds(), 1.0))

        # Execute snipe via OTReservationSniper
        _set_phase(SnipePhase.SNIPING, "Sniping OpenTable...")
        asyncio.create_task(db.update_reservation(reservation_id, status="sniping"))

        sniper = OTReservationSniper()
        result = await sniper.execute(
            config=config,
            auth_token=auth_token,
            dry_run=dry_run,
            user_id=user_id,
            local_address=local_ip,
        )

        # Update DB with result
        is_dry_run_success = dry_run and result.success
        update_fields = {
            "status": "dry_run" if is_dry_run_success else ("confirmed" if result.success else "failed"),
            "attempts": result.attempts,
            "elapsed_seconds": result.elapsed_seconds,
        }
        if result.ot_confirmation:
            update_fields["resy_token"] = f"OT:{result.ot_confirmation}"  # Store OT confirmation in resy_token field
        if result.error:
            update_fields["error"] = result.error
        await db.update_reservation(reservation_id, **update_fields)

        # Clean up slot claims
        from tablement.web.routes.reservations import _cleanup_claims
        await _cleanup_claims(reservation_id, update_fields["status"])

        # Stripe: charge on success
        if result.success and not is_dry_run_success:
            from tablement.web.routes.reservations import _handle_stripe_result
            asyncio.create_task(_handle_stripe_result(reservation_id, user_id, result))

        # Notify user
        if result.success and not is_dry_run_success:
            from tablement.web.routes.reservations import _notify_booking_confirmed
            asyncio.create_task(_notify_booking_confirmed(
                user_id=user_id,
                reservation_id=reservation_id,
                venue_name=config.venue_name or f"Venue {config.venue_id}",
                slot_time=result.slot.date.start if result.slot else "",
                target_date=config.date.isoformat(),
                party_size=config.party_size,
                resy_token=f"OT:{result.ot_confirmation}" if result.ot_confirmation else None,
            ))

        if is_dry_run_success:
            _set_phase(SnipePhase.DONE, "DRY RUN complete — slot found, booking NOT attempted")
        elif result.success:
            _set_phase(SnipePhase.DONE, f"Reservation confirmed! (OT #{result.ot_confirmation})")
        else:
            _set_phase(SnipePhase.DONE, result.error or "OT snipe failed")

        result_data = {
            "success": result.success,
            "dry_run": dry_run,
            "platform": "opentable",
            "attempts": result.attempts,
            "elapsed_seconds": result.elapsed_seconds,
            "error": result.error,
        }
        if result.ot_confirmation:
            result_data["ot_confirmation"] = result.ot_confirmation
        if result.slot:
            result_data["slot_time"] = result.slot.date.start
            result_data["slot_type"] = result.slot.config.type
        job.broadcast("snipe_result", result_data)

    except asyncio.CancelledError:
        _set_phase(SnipePhase.DONE, "Cancelled")
        await db.update_reservation(reservation_id, status="cancelled", error="Cancelled")
        from tablement.web.routes.reservations import _cleanup_claims
        await _cleanup_claims(reservation_id, "cancelled")
        job.broadcast("snipe_result", {
            "success": False, "error": "Cancelled by user",
            "attempts": job.status.attempt, "elapsed_seconds": 0,
        })
    except Exception as e:
        logger.exception("OT snipe %s failed with error", reservation_id)
        _set_phase(SnipePhase.DONE, f"Error: {e}")
        await db.update_reservation(reservation_id, status="failed", error=str(e))
        from tablement.web.routes.reservations import _cleanup_claims
        await _cleanup_claims(reservation_id, "failed")
        job.broadcast("snipe_result", {
            "success": False, "error": str(e),
            "attempts": job.status.attempt, "elapsed_seconds": 0,
        })
    finally:
        ip_pool.release(snipe_ip_key)
        # Auto-stop scout if this was the last snipe for the venue
        try:
            from tablement.web.routes.reservations import _maybe_stop_scout_for_venue
            await _maybe_stop_scout_for_venue(app, config.venue_id)
        except Exception:
            pass
        await asyncio.sleep(5)
        app.state.jobs.remove(reservation_id)


# ---------------------------------------------------------------------------
# OT Snipe from scheduler (entry point when APScheduler fires)
# ---------------------------------------------------------------------------


async def _run_ot_snipe_from_scheduler(
    app, reservation_id: str, user_id: str, config: SnipeConfig, profile: dict, dry_run: bool,
):
    """Entry point when APScheduler fires the OT job."""
    job = app.state.jobs.create(reservation_id, user_id)
    await _run_ot_snipe(app, reservation_id, user_id, config, profile, dry_run, job)


# ---------------------------------------------------------------------------
# OT Cancellation sniping — 30s minimum poll interval
# ---------------------------------------------------------------------------


async def _ot_cancellation_snipe_loop(
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
    """Continuous cancellation polling for OpenTable with 30s+ intervals.

    Key difference from Resy: OT enforces 30s minimum between polls,
    so our tier intervals are 30-45s (hot) and 45-90s (warm).

    Uses unauthenticated find_slots() for polling (scout mode).
    When a matching slot appears → authenticate with bearer token → lock → book.
    """
    selector = SlotSelector(window_minutes=config.window_minutes)
    day_str = config.date.isoformat()
    attempt = 0

    # Burst/rest tracking
    cancel_backoff = 0.0
    burst_start = time_module.monotonic()
    tier_range = OT_CANCEL_TIER_HOT if poll_tier == "hot" else OT_CANCEL_TIER_WARM
    burst_config = OT_CANCEL_BURST_HOT if poll_tier == "hot" else OT_CANCEL_BURST_WARM
    rest_config = OT_CANCEL_REST_HOT if poll_tier == "hot" else OT_CANCEL_REST_WARM
    burst_duration = random.uniform(*burst_config)

    try:
        # Pre-build auth token (validated at link time)
        auth_token = _build_ot_auth_token(profile)
    except ValueError as exc:
        logger.error("OT cancel snipe %s: no bearer token — %s", reservation_id, exc)
        await db.update_reservation(reservation_id, status="failed", error=str(exc))
        job.broadcast("snipe_result", {"success": False, "error": str(exc)})
        app.state.jobs.remove(reservation_id)
        return

    try:
        while True:
            # ---- 1. Check stop conditions ----
            target_date = date.fromisoformat(day_str)

            # Date passed
            if target_date < date.today():
                logger.info("OT cancel snipe %s: target date passed", reservation_id)
                await db.update_reservation(reservation_id, status="failed", error="Target date passed")
                from tablement.web.routes.reservations import _cleanup_claims
                await _cleanup_claims(reservation_id, "failed")
                job.broadcast("snipe_result", {"success": False, "error": "Target date passed"})
                break

            # Check if cancelled by group logic or user
            row = await db.get_reservation_by_id(reservation_id)
            if row and row["status"] in ("confirmed", "cancelled", "failed"):
                logger.info("OT cancel snipe %s: status=%s, stopping", reservation_id, row["status"])
                break

            # Check latest_notify_hours
            target_dt = datetime.combine(target_date, time(19, 0))
            hours_until = (target_dt - datetime.now()).total_seconds() / 3600
            if hours_until < latest_notify_hours:
                logger.info(
                    "OT cancel snipe %s: within %.1fh of reservation (limit=%.1fh), stopping",
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

            # Sleep hours: 1 AM - 9 AM ET
            if OT_CANCEL_SLEEP_START <= et_hour < OT_CANCEL_SLEEP_END:
                wake_time = et_now.replace(hour=OT_CANCEL_SLEEP_END, minute=0, second=0, microsecond=0)
                sleep_seconds = (wake_time - et_now).total_seconds()
                if sleep_seconds > 0:
                    job.status.message = f"Sleeping until 9 AM ET ({int(sleep_seconds // 60)}m)"
                    logger.info("OT cancel snipe %s: sleeping %dm until 9 AM ET",
                                reservation_id, int(sleep_seconds // 60))
                    await asyncio.sleep(sleep_seconds)
                continue

            # ---- 3. Determine current tier ----
            current_tier = _get_ot_cancel_tier(day_str, poll_tier == "hot")
            tier_range = OT_CANCEL_TIER_HOT if current_tier == "hot" else OT_CANCEL_TIER_WARM
            burst_config = OT_CANCEL_BURST_HOT if current_tier == "hot" else OT_CANCEL_BURST_WARM
            rest_config = OT_CANCEL_REST_HOT if current_tier == "hot" else OT_CANCEL_REST_WARM

            # ---- 4. Scout poll (unauthenticated OT find_slots) ----
            attempt += 1
            try:
                async with OpenTableApiClient(
                    auth_token=auth_token,
                    snipe_mode=False,
                    user_id=user_id,
                ) as scout_client:
                    slots_raw = await scout_client.find_slots(
                        restaurant_id=config.venue_id,
                        day=day_str,
                        party_size=config.party_size,
                        fast=True,
                    )

                # Update attempt count (non-blocking)
                asyncio.create_task(db.update_reservation(reservation_id, attempts=attempt))

                job.status.attempt = attempt
                job.status.slots_found = len(slots_raw) if slots_raw else 0
                job.status.message = f"OT Poll {attempt}: {len(slots_raw) if slots_raw else 0} slots ({current_tier})"

                if attempt <= 3 or attempt % 10 == 0 or (slots_raw and len(slots_raw) > 0):
                    job.broadcast("snipe_attempt", {
                        "attempt": attempt,
                        "slots_found": len(slots_raw) if slots_raw else 0,
                        "message": f"OT Poll {attempt}: {len(slots_raw) if slots_raw else 0} slots",
                    })

                # ---- 5. If slot found → booking burst ----
                if slots_raw:
                    # Convert OT slots to common Slot model for selector
                    common_slots = [s.to_common_slot() for s in slots_raw]
                    selected = selector.select(common_slots, config.time_preferences, config.date)

                    if selected:
                        logger.info("OT cancel snipe %s: found matching slot! Booking...", reservation_id)

                        # Booking burst: lock + book via authenticated client
                        slot_hash = selected.config.token
                        ot_datetime = selected.date.start.replace(" ", "T")
                        if ot_datetime.endswith(":00") and ot_datetime.count(":") == 2:
                            ot_datetime = ot_datetime[:16]

                        try:
                            async with OpenTableApiClient(
                                auth_token=auth_token,
                                snipe_mode=True,
                                user_id=user_id,
                            ) as booker:
                                # Small human delay
                                await asyncio.sleep(random.uniform(0.3, 1.0))

                                book_resp = await booker.book(
                                    restaurant_id=config.venue_id,
                                    slot_hash=slot_hash,
                                    party_size=config.party_size,
                                    date_time=ot_datetime,
                                )

                            if book_resp.confirmation_number:
                                # SUCCESS
                                logger.info(
                                    "OT cancel snipe %s: BOOKED! confirmation=%s",
                                    reservation_id, book_resp.confirmation_number,
                                )

                                await db.update_reservation(
                                    reservation_id,
                                    status="confirmed",
                                    resy_token=f"OT:{book_resp.confirmation_number}",
                                    attempts=attempt,
                                )
                                from tablement.web.routes.reservations import _cleanup_claims
                                await _cleanup_claims(reservation_id, "confirmed")

                                job.broadcast("snipe_result", {
                                    "success": True,
                                    "platform": "opentable",
                                    "ot_confirmation": book_resp.confirmation_number,
                                    "slot_time": selected.date.start,
                                    "slot_type": selected.config.type,
                                    "attempts": attempt,
                                    "elapsed_seconds": 0,
                                })

                                # Notify user
                                from tablement.web.routes.reservations import _notify_booking_confirmed
                                asyncio.create_task(_notify_booking_confirmed(
                                    user_id=user_id,
                                    reservation_id=reservation_id,
                                    venue_name=config.venue_name or f"Venue {config.venue_id}",
                                    slot_time=selected.date.start,
                                    target_date=config.date.isoformat(),
                                    party_size=config.party_size,
                                    resy_token=f"OT:{book_resp.confirmation_number}",
                                ))

                                # Auto-cancel group members
                                if group_id:
                                    from tablement.web.routes.reservations import _cancel_group_members
                                    await _cancel_group_members(app, group_id, reservation_id)

                                # Stripe charge
                                result = SnipeResult(
                                    success=True,
                                    platform="opentable",
                                    ot_confirmation=book_resp.confirmation_number,
                                    slot=selected,
                                    attempts=attempt,
                                    elapsed_seconds=0,
                                )
                                from tablement.web.routes.reservations import _handle_stripe_result
                                asyncio.create_task(_handle_stripe_result(reservation_id, user_id, result))
                                break
                            else:
                                logger.warning(
                                    "OT cancel snipe %s: booking returned no confirmation",
                                    reservation_id,
                                )

                        except Exception as book_err:
                            logger.warning(
                                "OT cancel snipe %s: booking failed: %s — retrying",
                                reservation_id, book_err,
                            )

            except Exception as e:
                logger.warning("OT cancel snipe %s poll %d failed: %s", reservation_id, attempt, e)
                cancel_backoff = min(max(cancel_backoff * 2, 1.0), 60.0)
                await asyncio.sleep(cancel_backoff)
                cancel_backoff = 0.0

            # ---- 6. Check if burst exceeded → rest period ----
            elapsed_burst = time_module.monotonic() - burst_start
            if elapsed_burst > burst_duration:
                rest_seconds = random.uniform(*rest_config)
                job.status.message = f"Resting {int(rest_seconds)}s (stealth pause)"
                logger.info(
                    "OT cancel snipe %s: burst %.0fs done, resting %.0fs",
                    reservation_id, elapsed_burst, rest_seconds,
                )
                await asyncio.sleep(rest_seconds)
                burst_start = time_module.monotonic()
                burst_duration = random.uniform(*burst_config)

            # ---- 7. Sleep for tier interval ----
            interval = random.uniform(*tier_range)

            # Late-night slowdown: 10 PM–midnight
            if OT_CANCEL_ACTIVE_END <= et_hour < 24:
                interval += random.uniform(*OT_CANCEL_SLOWDOWN_EXTRA)

            await asyncio.sleep(interval)

    except asyncio.CancelledError:
        await db.update_reservation(reservation_id, status="cancelled", error="Cancelled")
        from tablement.web.routes.reservations import _cleanup_claims
        await _cleanup_claims(reservation_id, "cancelled")
    finally:
        app.state.jobs.remove(reservation_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_ot_cancel_tier(target_date: str, is_priority: bool) -> str:
    """Determine polling tier for an OT cancellation snipe.

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
