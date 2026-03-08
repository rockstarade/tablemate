"""OpenTable scout — simplified monitoring for drop and cancellation events.

Simpler than Resy's 5-phase scout because:
  - OT mobile API has stricter rate limits (30s min between polls)
  - No Imperva cookies to warm (OT uses Akamai — handled by API client)
  - 3 phases: WARM (T-5min, Akamai prewarm) → POLL (T-0+, 30s intervals) → REPORT

Two scout types:
  OTDropScout         — monitors daily drop windows (e.g., 9 AM for Don Angie)
  OTCancellationScout — polls for cancellations with burst/rest pattern

These are used by the ScoutOrchestrator (web/scout.py) when the target
restaurant is on the OpenTable platform.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, date

from tablement.opentable.api import OpenTableApiClient
from tablement.opentable.models import OTAuthToken
from tablement.proxy import proxy_pool
from tablement.web import db

logger = logging.getLogger(__name__)

# OT drop polling: 30s minimum (OT's rate limit enforcement)
OT_DROP_POLL_INTERVAL = (30, 45)      # seconds between polls
OT_DROP_TIMEOUT_MINUTES = 10          # how long to poll after expected drop

# OT cancellation polling
OT_CANCEL_POLL_INTERVAL = (30, 60)    # seconds — 30s min (OT enforcement)
OT_CANCEL_BURST_DURATION = (15 * 60, 25 * 60)   # 15-25 min bursts
OT_CANCEL_REST_DURATION = (5 * 60, 10 * 60)     # 5-10 min rest

# Default party size for scout polls
OT_DEFAULT_PARTY_SIZE = 2


async def run_ot_drop_scout(
    campaign: dict,
    *,
    running_check: callable,
    now_et_fn: callable,
    record_snapshot_fn: callable,
    broadcast_drop_fn: callable,
    log_scout_error_fn: callable,
    local_ip: str | None = None,
) -> None:
    """OT drop scout — monitors daily drop windows.

    3 phases:
      1. WARM (T-5min): Pre-warm Akamai session
      2. POLL (T-0 to T+10min): Poll every 30-45s for new slots
      3. REPORT: Log results, sleep until tomorrow

    Args:
        campaign: Scout campaign dict from DB (venue_id, drop_hour, etc.)
        running_check: Callable that returns True if scout should keep running
        now_et_fn: Callable that returns current ET datetime
        record_snapshot_fn: Callable to record slot snapshots
        broadcast_drop_fn: Callable to broadcast drop detection to snipe subscribers
        log_scout_error_fn: Callable to log errors to DB
        local_ip: Optional IP to bind to for this scout
    """
    venue_id = campaign["venue_id"]
    venue_name = campaign.get("venue_name", f"OT Venue {venue_id}")
    drop_hour = campaign.get("drop_hour", 9)
    drop_minute = campaign.get("drop_minute", 0)
    days_ahead = campaign.get("days_ahead", 30)

    logger.info(
        "OT drop scout %s (%d): starting — drop=%d:%02d, days_ahead=%d",
        venue_name, venue_id, drop_hour, drop_minute, days_ahead,
    )

    # Build a minimal auth token for unauthenticated polling
    # (find_slots doesn't strictly require auth on OT mobile API)
    scout_token = OTAuthToken(bearer_token="scout-mode")

    while running_check():
        try:
            now_et = now_et_fn()

            # Calculate today's drop time
            drop_time = now_et.replace(
                hour=drop_hour, minute=drop_minute, second=0, microsecond=0,
            )
            timeout_end = drop_time + timedelta(minutes=OT_DROP_TIMEOUT_MINUTES)

            # If today's drop already passed, sleep until tomorrow
            if now_et > timeout_end:
                tomorrow_drop = drop_time + timedelta(days=1)
                sleep_s = (tomorrow_drop - now_et).total_seconds() - 300  # wake 5min early
                sleep_s = max(60, sleep_s)
                logger.info(
                    "OT drop scout %s: drop passed, sleeping %.0fm until tomorrow",
                    venue_name, sleep_s / 60,
                )
                await asyncio.sleep(sleep_s)
                continue

            # Calculate target date (drop date = today + days_ahead)
            target_date = (now_et.date() + timedelta(days=days_ahead)).isoformat()

            # Phase 1: WARM (T-5min) — pre-warm Akamai session
            warm_time = drop_time - timedelta(minutes=5)
            if now_et < warm_time:
                sleep_s = (warm_time - now_et).total_seconds()
                logger.info(
                    "OT drop scout %s: sleeping %.0fm until warmup",
                    venue_name, sleep_s / 60,
                )
                await asyncio.sleep(sleep_s)

            # Akamai warmup
            try:
                async with OpenTableApiClient(
                    auth_token=scout_token,
                    snipe_mode=False,
                    local_address=local_ip,
                ) as client:
                    await client.prewarm_akamai()
                    logger.info("OT drop scout %s: Akamai warmup complete", venue_name)
            except Exception as e:
                logger.warning("OT drop scout %s: warmup failed (non-critical): %s", venue_name, e)

            # Phase 2: POLL (T-0 to T+10min) — poll every 30-45s
            # Wait until drop time
            now_et = now_et_fn()
            if now_et < drop_time:
                await asyncio.sleep((drop_time - now_et).total_seconds())

            logger.info("OT drop scout %s: entering drop poll phase", venue_name)
            slots_detected = False
            poll_count = 0

            while now_et_fn() < timeout_end and running_check():
                poll_count += 1
                try:
                    async with OpenTableApiClient(
                        auth_token=scout_token,
                        snipe_mode=False,
                        local_address=local_ip,
                    ) as client:
                        slots = await client.find_slots(
                            restaurant_id=venue_id,
                            day=target_date,
                            party_size=OT_DEFAULT_PARTY_SIZE,
                            fast=True,
                        )

                    slot_count = len(slots) if slots else 0

                    if slot_count > 0 and not slots_detected:
                        slots_detected = True
                        logger.info(
                            "OT drop scout %s: DROP DETECTED! %d slots on poll %d",
                            venue_name, slot_count, poll_count,
                        )
                        # Broadcast to snipe subscribers
                        try:
                            broadcast_drop_fn(venue_id, slots)
                        except Exception as e:
                            logger.warning("Drop broadcast failed: %s", e)

                    # Record snapshot
                    try:
                        await record_snapshot_fn(venue_id, target_date, slot_count, slots)
                    except Exception:
                        pass

                except Exception as e:
                    logger.warning(
                        "OT drop scout %s poll %d failed: %s",
                        venue_name, poll_count, e,
                    )
                    try:
                        await log_scout_error_fn(venue_id, "drop", str(e))
                    except Exception:
                        pass

                # Sleep 30-45s between polls (OT rate limit)
                interval = random.uniform(*OT_DROP_POLL_INTERVAL)
                await asyncio.sleep(interval)

            # Phase 3: REPORT
            logger.info(
                "OT drop scout %s: poll phase complete — %d polls, slots_detected=%s",
                venue_name, poll_count, slots_detected,
            )

            # Sleep until tomorrow's drop minus 5 min
            now_et = now_et_fn()
            tomorrow_drop = drop_time + timedelta(days=1)
            sleep_s = (tomorrow_drop - now_et).total_seconds() - 300
            sleep_s = max(60, sleep_s)
            await asyncio.sleep(sleep_s)

        except asyncio.CancelledError:
            logger.info("OT drop scout %s cancelled", venue_name)
            raise
        except Exception as e:
            logger.exception("OT drop scout %s error: %s", venue_name, e)
            await asyncio.sleep(60)  # back off on unexpected errors


async def run_ot_cancellation_scout(
    campaign: dict,
    *,
    running_check: callable,
    now_et_fn: callable,
    record_snapshot_fn: callable,
    log_scout_error_fn: callable,
    local_ip: str | None = None,
) -> None:
    """OT cancellation scout — polls for cancellations with burst/rest pattern.

    Polls every 30-60s (OT rate limit), with burst/rest cycles:
      BURST: 15-25 min of polling
      REST:  5-10 min pause (looks natural)

    Only active during business hours (9 AM - 10 PM ET).
    """
    venue_id = campaign["venue_id"]
    venue_name = campaign.get("venue_name", f"OT Venue {venue_id}")
    days_ahead = campaign.get("days_ahead", 30)

    logger.info("OT cancellation scout %s (%d): starting", venue_name, venue_id)

    # Build a minimal auth token for unauthenticated polling
    scout_token = OTAuthToken(bearer_token="scout-mode")

    burst_start = time.monotonic()
    burst_duration = random.uniform(*OT_CANCEL_BURST_DURATION)
    poll_count = 0

    while running_check():
        try:
            now_et = now_et_fn()
            et_hour = now_et.hour

            # Sleep hours: 1 AM - 9 AM ET
            if 1 <= et_hour < 9:
                wake = now_et.replace(hour=9, minute=0, second=0, microsecond=0)
                sleep_s = (wake - now_et).total_seconds()
                if sleep_s > 0:
                    logger.info(
                        "OT cancel scout %s: sleeping %dm until 9 AM",
                        venue_name, int(sleep_s // 60),
                    )
                    await asyncio.sleep(sleep_s)
                continue

            # Calculate target dates (today through today + days_ahead)
            # Pick 2-3 dates to check (most likely to have cancellations)
            today = now_et.date()
            check_dates = []
            for offset in [1, 2, 3, 7, 14]:
                d = today + timedelta(days=offset)
                if d <= today + timedelta(days=days_ahead):
                    check_dates.append(d.isoformat())
            if not check_dates:
                check_dates = [(today + timedelta(days=1)).isoformat()]

            # Pick one date to poll (rotate)
            target_date = check_dates[poll_count % len(check_dates)]

            poll_count += 1
            try:
                async with OpenTableApiClient(
                    auth_token=scout_token,
                    snipe_mode=False,
                    local_address=local_ip,
                ) as client:
                    slots = await client.find_slots(
                        restaurant_id=venue_id,
                        day=target_date,
                        party_size=OT_DEFAULT_PARTY_SIZE,
                        fast=True,
                    )

                slot_count = len(slots) if slots else 0

                if slot_count > 0:
                    logger.info(
                        "OT cancel scout %s: %d slots found for %s (poll %d)",
                        venue_name, slot_count, target_date, poll_count,
                    )

                # Record snapshot periodically
                try:
                    await record_snapshot_fn(venue_id, target_date, slot_count, slots)
                except Exception:
                    pass

            except Exception as e:
                logger.warning(
                    "OT cancel scout %s poll %d failed: %s",
                    venue_name, poll_count, e,
                )
                try:
                    await log_scout_error_fn(venue_id, "cancellation", str(e))
                except Exception:
                    pass

            # Check if burst exceeded → rest period
            elapsed_burst = time.monotonic() - burst_start
            if elapsed_burst > burst_duration:
                rest_s = random.uniform(*OT_CANCEL_REST_DURATION)
                logger.info(
                    "OT cancel scout %s: burst %.0fs done, resting %.0fs",
                    venue_name, elapsed_burst, rest_s,
                )
                await asyncio.sleep(rest_s)
                burst_start = time.monotonic()
                burst_duration = random.uniform(*OT_CANCEL_BURST_DURATION)

            # Poll interval: 30-60s (OT rate limit)
            interval = random.uniform(*OT_CANCEL_POLL_INTERVAL)

            # Late-night slowdown: 10 PM–midnight, add extra
            if 22 <= et_hour < 24:
                interval += random.uniform(15, 45)

            await asyncio.sleep(interval)

        except asyncio.CancelledError:
            logger.info("OT cancellation scout %s cancelled", venue_name)
            raise
        except Exception as e:
            logger.exception("OT cancellation scout %s error: %s", venue_name, e)
            await asyncio.sleep(60)
