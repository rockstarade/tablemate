"""Core snipe orchestrator: timing, retry, and booking flow.

Performance optimizations:
- Single persistent HTTP/2 connection (auth → warmup → snipe)
- Non-blocking NTP offset compensation (runs in thread pool)
- Resy server clock calibration (adjusts pre-fire by server offset)
- Pre-fire strategy (start 200ms early, adjusted by clock offsets)
- Keep-alive pings every 10s between phases
- Earlier warmup at T-30s (was T-5s)
- Pre-built booking request templates
- Overlapping find_slots() volleys (catch slots the instant they go live)
- Fast book_token extraction via regex (skip full JSON parse)
- Dual-path booking (direct + proxy simultaneously)
- orjson fast parsing in snipe loop
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

import httpx

from tablement.api import ResyApiClient
from tablement.auth import AuthManager
from tablement.models import SnipeConfig, SnipeResult
from tablement.scheduler import PrecisionScheduler
from tablement.selector import SlotSelector

logger = logging.getLogger(__name__)

# Pre-fire offset: start first find_slots() 200ms before drop
PRE_FIRE_MS = 200

# Warmup timing
WARMUP_BEFORE_SECONDS = 30  # Was 5s — earlier warmup for better TCP window
PING_INTERVAL_SECONDS = 10  # Was 15s — more frequent keep-alive


class ReservationSniper:
    """
    Orchestrates the complete snipe flow:

    T-60s:  Authenticate (fresh token + payment_method_id)
    T-30s:  Warm TCP+TLS connection + calibrate Resy clock
    T-2s:   Switch to busy-wait for sub-ms precision
    T-0s:   Begin snipe loop: find → select → details → book
    """

    def __init__(
        self,
        auth_manager: AuthManager | None = None,
        scheduler: PrecisionScheduler | None = None,
        selector: SlotSelector | None = None,
    ) -> None:
        self.auth_manager = auth_manager or AuthManager()
        self.scheduler = scheduler or PrecisionScheduler()
        self.selector = selector or SlotSelector()

    async def execute(
        self, config: SnipeConfig, dry_run: bool = False
    ) -> SnipeResult:
        """Run the full snipe flow with a single persistent connection."""
        credentials = self.auth_manager.load_credentials()
        drop_time = self.scheduler.calculate_drop_datetime(config)
        day_str = config.date.isoformat()

        logger.info("Snipe target: %s on %s", config.venue_name or config.venue_id, day_str)
        logger.info("Drop time: %s", drop_time.isoformat())

        # Non-blocking NTP offset check (runs in thread, saves 2-5s)
        ntp_task = asyncio.create_task(self.scheduler.check_ntp_offset_async())

        # Single persistent connection for entire flow
        async with ResyApiClient() as client:
            # Authenticate at T-60s
            auth_time = drop_time - timedelta(seconds=60)

            # Collect NTP result (should be done by now)
            ntp_offset = await ntp_task
            if ntp_offset is not None:
                logger.info("NTP offset: %.1fms, compensating", ntp_offset * 1000)

            # Apply clock compensation
            drop_time = self.scheduler.compensate_drop_time(drop_time)

            await self.scheduler.wait_until(auth_time)

            logger.info("Authenticating...")
            token, payment_id = await self.auth_manager.login(client, credentials)

            # Reuse connection — just add auth headers
            client.set_auth_token(token)

            # Pre-build booking template (freeze headers + static params)
            client.prepare_book_template(payment_id)

            # Keep-alive pings until T-30 (was T-5)
            warmup_time = drop_time - timedelta(seconds=WARMUP_BEFORE_SECONDS)
            while True:
                remaining = (warmup_time - self.scheduler._now(drop_time.tzinfo)).total_seconds()
                if remaining <= 0:
                    break
                await client.ping()
                await asyncio.sleep(min(remaining, PING_INTERVAL_SECONDS))

            # Warmup at T-30s: warm proxy connection + calibrate Resy clock
            logger.info("Warming connection + calibrating clock...")
            warmup_coro = client.find_slots(
                venue_id=config.venue_id,
                day=day_str,
                party_size=config.party_size,
            )
            clock_coro = client.calibrate_resy_clock(samples=3)
            direct_coro = client.warmup_direct()

            # Run warmup, clock cal, and direct client warmup in parallel
            results = await asyncio.gather(
                warmup_coro, clock_coro, direct_coro,
                return_exceptions=True,
            )

            # Apply Resy clock offset
            resy_offset = results[1] if isinstance(results[1], float) else 0.0
            if resy_offset != 0.0:
                self.scheduler.set_resy_offset(resy_offset)
                drop_time = self.scheduler.compensate_drop_time(
                    self.scheduler.calculate_drop_datetime(config)
                )

            # Continue pinging until T-2s
            while True:
                remaining = (drop_time - self.scheduler._now(drop_time.tzinfo)).total_seconds()
                if remaining <= 2.0 + (PRE_FIRE_MS / 1000):
                    break
                await client.ping()
                await asyncio.sleep(min(remaining - 2.0 - (PRE_FIRE_MS / 1000), PING_INTERVAL_SECONDS))

            # Switch to snipe mode (tighter timeouts)
            client.set_snipe_mode(True)

            # Wait for drop time minus pre-fire offset
            pre_fire_target = drop_time - timedelta(milliseconds=PRE_FIRE_MS)
            await self.scheduler.wait_until(pre_fire_target)
            logger.info("GO! Starting snipe loop (pre-fire: %dms early)...", PRE_FIRE_MS)

            # Snipe loop
            return await self._snipe_loop(client, config, payment_id, dry_run)

    async def _snipe_loop(
        self,
        client: ResyApiClient,
        config: SnipeConfig,
        payment_id: int,
        dry_run: bool,
    ) -> SnipeResult:
        """Tight retry loop: find → select → details → dual_book.

        Optimized critical path:
        - Overlapping find_slots() volleys (catch slots instantly)
        - Fast book_token extraction via regex (skip full parse)
        - Dual-path booking (direct + proxy race)
        """
        start = time.monotonic()
        attempt = 0
        day_str = config.date.isoformat()

        while True:
            attempt += 1
            elapsed = time.monotonic() - start

            if elapsed > config.retry.duration_seconds:
                logger.warning("Retry window exhausted after %d attempts (%.1fs)", attempt, elapsed)
                return SnipeResult(
                    success=False,
                    attempts=attempt,
                    elapsed_seconds=elapsed,
                    error="Retry window exhausted",
                )

            if attempt > config.retry.max_attempts:
                logger.warning("Max attempts (%d) reached", config.retry.max_attempts)
                return SnipeResult(
                    success=False,
                    attempts=attempt,
                    elapsed_seconds=elapsed,
                    error="Max attempts reached",
                )

            try:
                # Step 1: Overlapping find_slots() volleys
                slots = await client.find_slots_rapid(
                    venue_id=config.venue_id,
                    day=day_str,
                    party_size=config.party_size,
                )

                if not slots:
                    logger.debug("Attempt %d: no slots yet", attempt)
                    if config.retry.interval_seconds > 0:
                        await asyncio.sleep(config.retry.interval_seconds)
                    continue

                logger.info("Attempt %d: found %d slots", attempt, len(slots))

                # Step 2: Select best slot
                selected = self.selector.select(
                    slots, config.time_preferences, config.date
                )
                if not selected:
                    logger.info("No slot matches preferences, retrying...")
                    continue

                logger.info(
                    "Selected: %s %s (%s)",
                    selected.date.start,
                    selected.config.type,
                    selected.config.token[:20] + "...",
                )

                if dry_run:
                    return SnipeResult(
                        success=True,
                        slot=selected,
                        attempts=attempt,
                        elapsed_seconds=time.monotonic() - start,
                        error="DRY RUN — booking skipped",
                    )

                # Step 3: Get book token (TIME CRITICAL — fast regex extraction)
                book_token = await client.get_details_fast(
                    config_id=selected.config.token,
                    day=day_str,
                    party_size=config.party_size,
                )

                # Step 4: Dual-path book (direct + proxy race)
                result = await client.dual_book(
                    book_token=book_token,
                    payment_method_id=payment_id,
                )

                elapsed = time.monotonic() - start
                logger.info(
                    "BOOKED in %.3fs after %d attempts! Token: %s",
                    elapsed,
                    attempt,
                    result.resy_token,
                )
                return SnipeResult(
                    success=True,
                    resy_token=result.resy_token,
                    slot=selected,
                    attempts=attempt,
                    elapsed_seconds=elapsed,
                )

            except httpx.HTTPStatusError as e:
                logger.warning(
                    "Attempt %d: HTTP %d — %s",
                    attempt,
                    e.response.status_code,
                    e.response.text[:200],
                )
                continue
            except Exception as e:
                logger.error("Attempt %d: unexpected error: %s", attempt, e)
                continue
