"""OpenTable snipe orchestrator — parallel to tablement.sniper (Resy).

Orchestrates the full snipe timeline:
  T-40-90s:  Validate bearer token
  T-30s:     Warmup (TCP+TLS + Akamai + clock calibration)
  T-2s:      Busy-wait spin (reuses shared PrecisionScheduler)
  T-0:       Snipe loop: find → select → lock → book

Key differences from Resy sniper:
  - 3-step booking (find → lock → book) vs 4-step (find → details → book_token → book)
  - Lock has a short expiry window — must book immediately after locking
  - No dual-path booking (lock+book must be sequential, not raced)
  - Rate limits are stricter (30s min between polls in monitor mode)
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from tablement.errors import ExhaustedRetriesError, OTLockError
from tablement.models import SnipeConfig, SnipeResult
from tablement.opentable.api import OpenTableApiClient
from tablement.opentable.models import OTAuthToken
from tablement.selector import SlotSelector

logger = logging.getLogger(__name__)


class OTReservationSniper:
    """Orchestrates the OpenTable snipe flow.

    Usage:
        sniper = OTReservationSniper()
        result = await sniper.execute(config, auth_token)
    """

    def __init__(
        self,
        selector: SlotSelector | None = None,
    ) -> None:
        self._selector = selector or SlotSelector()

    async def execute(
        self,
        config: SnipeConfig,
        auth_token: OTAuthToken,
        *,
        dry_run: bool = False,
        user_id: str | None = None,
        local_address: str | None = None,
    ) -> SnipeResult:
        """Execute the full OpenTable snipe sequence.

        Args:
            config:        Snipe configuration (venue, date, times, drop_time)
            auth_token:    OT bearer token + identity
            dry_run:       If True, find slots but don't book
            user_id:       For proxy/fingerprint assignment
            local_address: For IP binding

        Returns:
            SnipeResult with success status and confirmation.
        """
        t_start = time.monotonic()

        async with OpenTableApiClient(
            auth_token=auth_token,
            snipe_mode=True,
            user_id=user_id,
            local_address=local_address,
        ) as client:

            # ── Phase 1: Validate token ──────────────────────
            logger.info("OT snipe: validating bearer token...")
            try:
                await client.validate_token()
                logger.info("OT snipe: token valid")
            except Exception as exc:
                logger.error("OT snipe: token validation failed: %s", exc)
                return SnipeResult(
                    success=False,
                    platform="opentable",
                    error=f"Token validation failed: {exc}",
                    elapsed_seconds=time.monotonic() - t_start,
                )

            # ── Phase 2: Warmup ──────────────────────────────
            logger.info("OT snipe: warming up...")
            warmup_tasks = [
                client.prewarm_akamai(),
                client.calibrate_ot_clock(),
            ]
            await asyncio.gather(*warmup_tasks, return_exceptions=True)

            # Keep-alive pings until snipe window
            # (In production, the caller handles timing — we just snipe immediately)

            # ── Phase 3: Snipe loop ──────────────────────────
            logger.info("OT snipe: starting snipe loop...")
            return await self._snipe_loop(
                client=client,
                config=config,
                auth_token=auth_token,
                dry_run=dry_run,
                t_start=t_start,
            )

    async def _snipe_loop(
        self,
        client: OpenTableApiClient,
        config: SnipeConfig,
        auth_token: OTAuthToken,
        dry_run: bool,
        t_start: float,
    ) -> SnipeResult:
        """Core snipe loop: find → select → lock → book.

        Runs in a tight loop for ``config.retry.duration_seconds``
        (default 10s), retrying on failure.
        """
        day_str = config.date.isoformat()
        deadline = time.monotonic() + config.retry.duration_seconds
        attempt = 0

        while time.monotonic() < deadline and attempt < config.retry.max_attempts:
            attempt += 1
            elapsed = time.monotonic() - t_start

            try:
                # ── Step 1: Find slots ────────────────────────
                slots_raw = await client.find_slots(
                    restaurant_id=config.venue_id,
                    day=day_str,
                    party_size=config.party_size,
                    fast=True,
                )

                if not slots_raw:
                    logger.debug(
                        "OT snipe attempt %d: no slots found (%.1fs)",
                        attempt, elapsed,
                    )
                    if config.retry.interval_seconds > 0:
                        await asyncio.sleep(config.retry.interval_seconds)
                    continue

                # ── Step 2: Select best slot ──────────────────
                # Convert OT slots to shared Slot model for SlotSelector
                common_slots = [s.to_common_slot() for s in slots_raw]
                selected = self._selector.select(
                    common_slots,
                    config.time_preferences,
                    config.date,
                )

                if not selected:
                    logger.debug(
                        "OT snipe attempt %d: %d slots found but none match prefs",
                        attempt, len(slots_raw),
                    )
                    continue

                logger.info(
                    "OT snipe attempt %d: selected slot %s (%s) at %.1fs",
                    attempt,
                    selected.date.start,
                    selected.config.type,
                    elapsed,
                )

                if dry_run:
                    logger.info("OT snipe: DRY RUN — skipping booking")
                    return SnipeResult(
                        success=True,
                        platform="opentable",
                        slot=selected,
                        attempts=attempt,
                        elapsed_seconds=time.monotonic() - t_start,
                    )

                # ── Step 3: Lock + Book ───────────────────────
                # The slot_hash is stored in config.token (via to_common_slot)
                slot_hash = selected.config.token
                # Reconstruct OT datetime from the slot
                ot_datetime = selected.date.start.replace(" ", "T")
                if ot_datetime.endswith(":00") and ot_datetime.count(":") == 2:
                    ot_datetime = ot_datetime[:16]  # "2026-03-26T19:00"

                try:
                    book_resp = await client.book(
                        restaurant_id=config.venue_id,
                        slot_hash=slot_hash,
                        party_size=config.party_size,
                        date_time=ot_datetime,
                    )
                except OTLockError as exc:
                    logger.warning(
                        "OT snipe attempt %d: lock failed: %s — retrying",
                        attempt, exc,
                    )
                    continue

                if book_resp.confirmation_number:
                    logger.info(
                        "OT snipe SUCCESS: confirmation=%s at attempt %d (%.1fs)",
                        book_resp.confirmation_number,
                        attempt,
                        time.monotonic() - t_start,
                    )
                    return SnipeResult(
                        success=True,
                        platform="opentable",
                        ot_confirmation=book_resp.confirmation_number,
                        slot=selected,
                        attempts=attempt,
                        elapsed_seconds=time.monotonic() - t_start,
                    )
                else:
                    logger.warning(
                        "OT snipe attempt %d: booking returned no confirmation",
                        attempt,
                    )

            except Exception as exc:
                logger.warning(
                    "OT snipe attempt %d error: %s", attempt, exc,
                )
                if config.retry.interval_seconds > 0:
                    await asyncio.sleep(config.retry.interval_seconds)

        # Exhausted retries
        return SnipeResult(
            success=False,
            platform="opentable",
            attempts=attempt,
            elapsed_seconds=time.monotonic() - t_start,
            error="Exhausted retries without booking",
        )
