"""Snipe execution API routes with SSE streaming.

Performance optimizations:
- Single persistent HTTP/2 connection (auth → warmup → snipe)
- NTP offset compensation for precise timing
- Pre-fire strategy: start find_slots() ~200ms before drop
- Keep-alive pings to prevent connection idle timeout
- orjson + fast mode for minimal parsing overhead
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as time_module
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from tablement.api import ResyApiClient
from tablement.models import DropTime, SnipeConfig, SnipeResult, TimePreference
from tablement.scheduler import PrecisionScheduler
from tablement.selector import SlotSelector
from tablement.web.schemas import (
    SnipeResultOut,
    SnipeStartRequest,
    SnipeStartResponse,
    SnipeStatusResponse,
)
from tablement.web.state import SessionState, SnipePhase, SnipeStatus

logger = logging.getLogger(__name__)
router = APIRouter()

# How early to fire the first find_slots() before drop time.
# Network round-trip is ~30-80ms from home, so 200ms means the request
# arrives at the server right around T-0.
PRE_FIRE_MS = 200


@router.post("/start", response_model=SnipeStartResponse)
async def start(body: SnipeStartRequest, request: Request):
    session: SessionState = request.app.state.session

    if not session.auth_token:
        raise HTTPException(401, "Not authenticated. Please log in first.")

    if session.snipe_status.phase not in (SnipePhase.IDLE, SnipePhase.DONE):
        raise HTTPException(409, "A snipe is already running.")

    # Build SnipeConfig
    config = SnipeConfig(
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
        ),
    )

    scheduler = PrecisionScheduler()
    drop_dt = scheduler.calculate_drop_datetime(config)

    session.reset_snipe()
    session.snipe_status.drop_datetime_iso = drop_dt.isoformat()

    session.snipe_task = asyncio.create_task(
        _run_snipe(session, config, body.dry_run)
    )

    return SnipeStartResponse(
        started=True,
        drop_datetime_iso=drop_dt.isoformat(),
        message=f"Snipe scheduled for {drop_dt.isoformat()}",
    )


@router.post("/cancel")
async def cancel(request: Request):
    session: SessionState = request.app.state.session
    session.reset_snipe()
    return {"cancelled": True}


@router.get("/status", response_model=SnipeStatusResponse)
async def status(request: Request):
    session: SessionState = request.app.state.session
    s = session.snipe_status
    result_out = _make_result_out(s.result) if s.result else None
    return SnipeStatusResponse(
        phase=s.phase.value,
        attempt=s.attempt,
        slots_found=s.slots_found,
        message=s.message,
        result=result_out,
        drop_datetime_iso=s.drop_datetime_iso,
    )


@router.get("/events")
async def events(request: Request):
    session: SessionState = request.app.state.session
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    session.event_queues.append(queue)

    async def generate():
        try:
            # Send current state on connect
            s = session.snipe_status
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
                # Stop streaming after result
                if msg["event"] == "snipe_result":
                    break
        except asyncio.CancelledError:
            pass
        finally:
            if queue in session.event_queues:
                session.event_queues.remove(queue)

    return EventSourceResponse(generate())


def _make_result_out(result: SnipeResult) -> SnipeResultOut:
    return SnipeResultOut(
        success=result.success,
        resy_token=result.resy_token,
        slot_time=result.slot.date.start if result.slot else None,
        slot_type=result.slot.config.type if result.slot else None,
        attempts=result.attempts,
        elapsed_seconds=result.elapsed_seconds,
        error=result.error,
    )


async def _run_snipe(session: SessionState, config: SnipeConfig, dry_run: bool):
    """Background task: orchestrate the snipe with SSE broadcasts.

    Uses a SINGLE persistent HTTP/2 connection for the entire flow:
    auth → keep-alive → warmup → snipe. This avoids wasting the TCP+TLS
    handshake and HTTP/2 session negotiation.
    """
    scheduler = PrecisionScheduler()
    selector = SlotSelector()
    drop_dt = scheduler.calculate_drop_datetime(config)
    day_str = config.date.isoformat()

    def _set_phase(phase: SnipePhase, message: str, **kw):
        session.snipe_status.phase = phase
        session.snipe_status.message = message
        for k, v in kw.items():
            setattr(session.snipe_status, k, v)
        session.broadcast("snipe_phase", {"phase": phase.value, "message": message, **kw})

    try:
        # --- NTP offset compensation ---
        ntp_offset = scheduler.check_ntp_offset()
        if ntp_offset is not None:
            abs_offset_ms = abs(ntp_offset * 1000)
            logger.info("NTP offset: %.1fms", ntp_offset * 1000)
            if abs_offset_ms > 100:
                logger.warning("Clock drift >100ms (%.0fms), compensating", abs_offset_ms)
            # Compensate: if our clock is ahead by +offset, we need to wait longer
            drop_dt = drop_dt - timedelta(seconds=ntp_offset)
            _set_phase(
                SnipePhase.WAITING,
                f"Waiting until {drop_dt.strftime('%H:%M:%S %Z')} "
                f"(NTP offset: {ntp_offset * 1000:+.0f}ms)",
            )
        else:
            _set_phase(SnipePhase.WAITING, f"Waiting until {drop_dt.strftime('%H:%M:%S %Z')}")

        # --- Single persistent connection for entire flow ---
        async with ResyApiClient() as client:
            # Countdown loop until T-60
            auth_time = drop_dt - timedelta(seconds=60)
            while True:
                now = datetime.now(drop_dt.tzinfo)
                remaining = (drop_dt - now).total_seconds()
                if (auth_time - now).total_seconds() <= 0:
                    break
                session.broadcast("countdown_tick", {
                    "seconds_remaining": remaining,
                    "phase": "waiting",
                })
                await asyncio.sleep(min((auth_time - now).total_seconds(), 1.0))

            # --- Authenticate at T-60 (reuses this connection) ---
            _set_phase(SnipePhase.AUTHENTICATING, "Authenticating with Resy...")
            auth_resp = await client.authenticate(
                session.resy_email, session.resy_password
            )
            pm = next(
                (p for p in auth_resp.payment_methods if p.is_default),
                auth_resp.payment_methods[0],
            )
            token = auth_resp.token
            payment_id = pm.id

            # Add auth headers to the SAME client — reuse TCP+TLS+H2 connection
            client.set_auth_token(token)
            logger.info("Auth complete, reusing connection for warmup+snipe")

            # --- Keep-alive pings until T-5 ---
            warmup_time = drop_dt - timedelta(seconds=5)
            last_ping = time_module.monotonic()
            while True:
                now = datetime.now(drop_dt.tzinfo)
                remaining = (drop_dt - now).total_seconds()
                if (warmup_time - now).total_seconds() <= 0:
                    break
                session.broadcast("countdown_tick", {
                    "seconds_remaining": remaining,
                    "phase": "authenticating",
                })
                # Keep-alive ping every 15 seconds
                if time_module.monotonic() - last_ping > 15:
                    await client.ping()
                    last_ping = time_module.monotonic()
                    logger.debug("Keep-alive ping sent")
                await asyncio.sleep(min((warmup_time - now).total_seconds(), 1.0))

            # --- Warmup at T-5 (connection already warm from auth+pings) ---
            _set_phase(SnipePhase.WARMING, "Warming connection...")
            try:
                await client.find_slots(config.venue_id, day_str, config.party_size)
            except Exception:
                pass

            # --- Switch to snipe mode: tighter timeouts ---
            client.set_snipe_mode(True)

            # Countdown final seconds
            while True:
                now = datetime.now(drop_dt.tzinfo)
                remaining = (drop_dt - now).total_seconds()
                # Pre-fire: stop waiting 200ms early
                if remaining <= 2.0 + (PRE_FIRE_MS / 1000):
                    break
                session.broadcast("countdown_tick", {
                    "seconds_remaining": remaining,
                    "phase": "warming",
                })
                await asyncio.sleep(min(remaining - 2.0 - (PRE_FIRE_MS / 1000), 1.0))

            # Busy-wait for final precision (in thread to not block event loop)
            remaining = (drop_dt - datetime.now(drop_dt.tzinfo)).total_seconds()
            # Subtract pre-fire window: fire 200ms early
            remaining -= PRE_FIRE_MS / 1000
            if remaining > 0:
                await asyncio.to_thread(_busy_wait, remaining)

            # SNIPE! (we're now at T-200ms, first request arrives ~T-0)
            _set_phase(SnipePhase.SNIPING, "GO! Searching for slots...")

            result = await _snipe_loop(
                client, config, selector, payment_id, dry_run, session
            )

            session.snipe_status.result = result
            if result.success:
                _set_phase(SnipePhase.DONE, "Reservation confirmed!")
            else:
                _set_phase(SnipePhase.DONE, result.error or "Snipe failed")

            session.broadcast("snipe_result", _make_result_out(result).model_dump())

    except asyncio.CancelledError:
        _set_phase(SnipePhase.DONE, "Cancelled")
        session.broadcast("snipe_result", {
            "success": False, "error": "Cancelled by user",
            "attempts": session.snipe_status.attempt, "elapsed_seconds": 0,
        })
    except Exception as e:
        logger.exception("Snipe failed with error")
        _set_phase(SnipePhase.DONE, f"Error: {e}")
        session.broadcast("snipe_result", {
            "success": False, "error": str(e),
            "attempts": session.snipe_status.attempt, "elapsed_seconds": 0,
        })


def _busy_wait(seconds: float):
    """Busy-wait for sub-ms precision. Runs in a thread."""
    target = time_module.monotonic() + seconds
    while time_module.monotonic() < target:
        pass


async def _snipe_loop(
    client: ResyApiClient,
    config: SnipeConfig,
    selector: SlotSelector,
    payment_id: int,
    dry_run: bool,
    session: SessionState,
) -> SnipeResult:
    """Tight retry loop: find -> select -> details -> book.

    Uses fast=True for orjson parsing and minimal Pydantic validation.
    """
    start = time_module.monotonic()
    attempt = 0
    day_str = config.date.isoformat()

    while True:
        attempt += 1
        elapsed = time_module.monotonic() - start

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
            # fast=True: orjson + skip full Pydantic tree validation
            slots = await client.find_slots(
                config.venue_id, day_str, config.party_size, fast=True
            )

            session.snipe_status.attempt = attempt
            session.snipe_status.slots_found = len(slots)
            session.broadcast("snipe_attempt", {
                "attempt": attempt,
                "slots_found": len(slots),
                "message": f"Attempt {attempt}: {len(slots)} slots",
            })

            if not slots:
                if config.retry.interval_seconds > 0:
                    await asyncio.sleep(config.retry.interval_seconds)
                continue

            selected = selector.select(slots, config.time_preferences, config.date)
            if not selected:
                continue

            if dry_run:
                return SnipeResult(
                    success=True, slot=selected, attempts=attempt,
                    elapsed_seconds=time_module.monotonic() - start,
                    error="DRY RUN - booking skipped",
                )

            details = await client.get_details(
                config_id=selected.config.token,
                day=day_str,
                party_size=config.party_size,
            )

            result = await client.book(
                book_token=details.book_token.value,
                payment_method_id=payment_id,
            )

            return SnipeResult(
                success=True,
                resy_token=result.resy_token,
                slot=selected,
                attempts=attempt,
                elapsed_seconds=time_module.monotonic() - start,
            )

        except Exception as e:
            logger.warning("Attempt %d failed: %s", attempt, e)
            continue
