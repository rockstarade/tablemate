"""Drop Intelligence collector — background service that records drop timing data.

Runs as a background task on the FastAPI server. For each tracked venue:
1. Calculates when the next drop should happen (based on venue's drop_hour/minute/days_ahead)
2. At T-5s before expected drop, starts aggressive polling (jittered 30-60ms)
3. Records EVERY poll response in an in-memory buffer, batch-inserts after
4. Records the exact timestamp when slots transition from 0 → N
5. Takes velocity snapshots to measure how fast slots disappear (early termination)

Polling tiers:
- 'aggressive': 1 poll/sec continuous monitoring (24/7) + drop-window burst
- 'passive': only poll during the drop window (T-5s to T+15s)

This data builds a competitive intelligence moat:
- Know that Semma consistently drops at 10:00:00.150 (150ms after the minute)
- Know that 4 Charles slots go from 30→0 in 800ms
- Tune pre-fire timing per-restaurant based on real data
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta, date

from tablement.api import ResyApiClient
from tablement.web import db

logger = logging.getLogger(__name__)

# Polling config during drop window
DROP_POLL_WINDOW_BEFORE_S = 5.0    # Start polling 5s before expected drop
DROP_POLL_WINDOW_AFTER_S = 15.0    # Keep polling 15s after expected drop
DROP_POLL_MIN_MS = 30              # Min interval between polls
DROP_POLL_MAX_MS = 60              # Max interval (jittered)

# Post-drop velocity: aggressive early snapshots, stop when slots gone
VELOCITY_SNAPSHOT_OFFSETS_S = [0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0]

# Continuous monitoring interval for aggressive tier (seconds)
AGGRESSIVE_POLL_INTERVAL_S = 1.0
AGGRESSIVE_POLL_JITTER_S = 0.15    # ±150ms jitter


class DropIntelCollector:
    """Manages background drop intelligence collection for all tracked venues."""

    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}  # venue_id → task
        self._running = False

    async def start(self, app) -> None:
        """Start the collector. Call from app lifespan startup."""
        self._running = True
        self._app = app
        logger.info("Drop Intelligence collector starting...")

        # Schedule daily refresh of tracked venues
        asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        """Stop all collection tasks."""
        self._running = False
        for venue_id, task in self._tasks.items():
            task.cancel()
        self._tasks.clear()
        logger.info("Drop Intelligence collector stopped")

    async def _refresh_loop(self) -> None:
        """Periodically refresh tracked venues and schedule/cancel observers."""
        while self._running:
            try:
                venues = await db.list_tracked_venues(active_only=True)
                active_ids = {v["venue_id"] for v in venues}

                # Cancel tasks for removed/deactivated venues
                for vid in list(self._tasks):
                    if vid not in active_ids:
                        self._tasks[vid].cancel()
                        del self._tasks[vid]
                        logger.info("Stopped tracking venue %d", vid)

                # Start tasks for new venues
                for venue in venues:
                    vid = venue["venue_id"]
                    if vid not in self._tasks or self._tasks[vid].done():
                        poll_tier = venue.get("poll_tier", "passive")
                        if poll_tier == "aggressive":
                            self._tasks[vid] = asyncio.create_task(
                                self._aggressive_observer(venue)
                            )
                            logger.info(
                                "Tracking venue %d (%s) AGGRESSIVE — 1 req/s + drop at %02d:%02d %s",
                                vid, venue.get("venue_name", "?"),
                                venue["drop_hour"], venue["drop_minute"],
                                venue["drop_timezone"],
                            )
                        else:
                            self._tasks[vid] = asyncio.create_task(
                                self._venue_observer(venue)
                            )
                            logger.info(
                                "Tracking venue %d (%s) PASSIVE — drop at %02d:%02d %s, %dd ahead",
                                vid, venue.get("venue_name", "?"),
                                venue["drop_hour"], venue["drop_minute"],
                                venue["drop_timezone"], venue["days_ahead"],
                            )

            except Exception as e:
                logger.warning("Drop intel refresh failed: %s", e)

            # Refresh every 5 minutes
            await asyncio.sleep(300)

    # ------------------------------------------------------------------
    # PASSIVE OBSERVER — only polls during drop window
    # ------------------------------------------------------------------

    async def _venue_observer(self, venue: dict) -> None:
        """Long-running observer for a single venue. Wakes up for each daily drop."""
        venue_id = venue["venue_id"]
        venue_name = venue.get("venue_name", "Unknown")

        while self._running:
            try:
                # Calculate next drop datetime
                next_drop = self._next_drop_time(venue)
                if next_drop is None:
                    await asyncio.sleep(3600)  # Retry in an hour
                    continue

                now = datetime.now(next_drop.tzinfo)
                wait_until = next_drop - timedelta(seconds=DROP_POLL_WINDOW_BEFORE_S)
                wait_secs = (wait_until - now).total_seconds()

                if wait_secs > 0:
                    logger.info(
                        "Venue %d (%s): next drop at %s (in %.0f min)",
                        venue_id, venue_name,
                        next_drop.strftime("%H:%M:%S %Z"),
                        wait_secs / 60,
                    )
                    # Sleep until T-5s, waking every 60s to check if still active
                    while wait_secs > 0 and self._running:
                        sleep_time = min(wait_secs, 60.0)
                        await asyncio.sleep(sleep_time)
                        wait_secs = (
                            wait_until - datetime.now(next_drop.tzinfo)
                        ).total_seconds()

                if not self._running:
                    break

                # RUN THE DROP OBSERVATION
                target_date = (
                    next_drop.date() + timedelta(days=venue["days_ahead"])
                ).isoformat()
                observation, poll_buffer = await self._observe_drop(
                    venue_id=venue_id,
                    venue_name=venue_name,
                    party_size=venue.get("party_size", 2),
                    target_date=target_date,
                    expected_drop=next_drop,
                )

                # Flush poll buffer (every poll recorded in memory)
                if poll_buffer:
                    try:
                        count = await db.batch_insert_slot_snapshots(poll_buffer)
                        logger.info(
                            "Flushed %d collector poll snapshots for %s (%d)",
                            count, venue_name, venue_id,
                        )
                    except Exception as e:
                        logger.warning("Collector poll buffer flush failed: %s", e)

                if observation:
                    logger.info(
                        "DROP DETECTED: %s (%d) — offset: %.1fms, %d slots",
                        venue_name, venue_id,
                        observation["offset_ms"],
                        observation["slots_found"],
                    )

                    # Start velocity tracking in background
                    asyncio.create_task(
                        self._track_velocity(
                            venue_id, venue_name,
                            venue.get("party_size", 2),
                            target_date, observation,
                        )
                    )
                else:
                    logger.info(
                        "No drop detected for %s (%d) at %s",
                        venue_name, venue_id, next_drop.strftime("%H:%M:%S"),
                    )

                # Wait until after the drop window + velocity tracking before next cycle
                await asyncio.sleep(VELOCITY_SNAPSHOT_OFFSETS_S[-1] + 60)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Venue observer %d error: %s", venue_id, e)
                await asyncio.sleep(60)  # Retry after a minute

    # ------------------------------------------------------------------
    # AGGRESSIVE OBSERVER — 1 req/s continuous + drop-window burst
    # ------------------------------------------------------------------

    async def _aggressive_observer(self, venue: dict) -> None:
        """Continuous 1 req/s monitoring with drop-window burst for aggressive tier.

        Outside drop window: polls at 1 req/s, saves each response to DB.
        During drop window: switches to 30-60ms burst polling (same as passive).
        After drop: velocity tracking with early termination.
        """
        venue_id = venue["venue_id"]
        venue_name = venue.get("venue_name", "Unknown")
        party_size = venue.get("party_size", 2)

        while self._running:
            try:
                next_drop = self._next_drop_time(venue)
                if next_drop is None:
                    await asyncio.sleep(3600)
                    continue

                now = datetime.now(next_drop.tzinfo)
                drop_window_start = next_drop - timedelta(seconds=DROP_POLL_WINDOW_BEFORE_S)

                # Calculate target_date for this drop
                target_date = (
                    next_drop.date() + timedelta(days=venue["days_ahead"])
                ).isoformat()

                # CONTINUOUS POLLING at 1 req/s until drop window
                logger.info(
                    "Aggressive monitor %d (%s): continuous 1/s until drop at %s",
                    venue_id, venue_name, next_drop.strftime("%H:%M:%S %Z"),
                )

                async with ResyApiClient(scout=True, user_id=f"intel-agg-{venue_id}") as client:
                    # Phase 1: Continuous 1 req/s until drop window
                    while self._running:
                        now = datetime.now(next_drop.tzinfo)
                        if now >= drop_window_start:
                            break  # Switch to burst mode

                        try:
                            slots = await client.find_slots(
                                venue_id, target_date, party_size, fast=True,
                            )
                            # Fire-and-forget DB insert (safe at 1 req/s)
                            asyncio.create_task(self._save_continuous_snapshot(
                                venue_id, target_date, len(slots), slots,
                            ))
                        except Exception:
                            pass

                        # 1 req/s with small jitter
                        jitter = random.uniform(
                            -AGGRESSIVE_POLL_JITTER_S, AGGRESSIVE_POLL_JITTER_S
                        )
                        await asyncio.sleep(AGGRESSIVE_POLL_INTERVAL_S + jitter)

                    if not self._running:
                        break

                    # Phase 2: Drop window burst (same as passive _observe_drop)
                    logger.info(
                        "Aggressive monitor %d (%s): entering drop window burst",
                        venue_id, venue_name,
                    )
                    observation, poll_buffer = await self._observe_drop(
                        venue_id=venue_id,
                        venue_name=venue_name,
                        party_size=party_size,
                        target_date=target_date,
                        expected_drop=next_drop,
                    )

                    # Flush burst poll buffer
                    if poll_buffer:
                        try:
                            count = await db.batch_insert_slot_snapshots(poll_buffer)
                            logger.info(
                                "Flushed %d burst poll snapshots for %s", count, venue_name,
                            )
                        except Exception:
                            pass

                    if observation:
                        logger.info(
                            "AGGRESSIVE DROP: %s (%d) — offset: %.1fms, %d slots",
                            venue_name, venue_id,
                            observation["offset_ms"],
                            observation["slots_found"],
                        )
                        # Velocity tracking
                        asyncio.create_task(
                            self._track_velocity(
                                venue_id, venue_name, party_size,
                                target_date, observation,
                            )
                        )

                # Wait before next cycle (velocity tracking + buffer)
                await asyncio.sleep(VELOCITY_SNAPSHOT_OFFSETS_S[-1] + 30)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Aggressive observer %d error: %s", venue_id, e)
                await asyncio.sleep(60)

    async def _save_continuous_snapshot(
        self, venue_id: int, target_date: str, slot_count: int, slots: list,
    ) -> None:
        """Save a single continuous monitoring snapshot. Fire-and-forget."""
        try:
            snapshot = {
                "venue_id": venue_id,
                "target_date": target_date,
                "slot_count": slot_count,
                "source": "collector",
            }
            if slot_count > 0 and slots:
                snapshot["slots_json"] = [
                    {"time": s.date.start, "type": s.config.type}
                    for s in slots[:50]
                ]
            await db.insert_slot_snapshot(snapshot)
        except Exception:
            pass  # Non-critical, don't spam logs at 1/s

    # ------------------------------------------------------------------
    # DROP OBSERVATION — burst polling with in-memory buffer
    # ------------------------------------------------------------------

    async def _observe_drop(
        self,
        venue_id: int,
        venue_name: str,
        party_size: int,
        target_date: str,
        expected_drop: datetime,
    ) -> tuple[dict | None, list[dict]]:
        """Aggressive polling around expected drop time.

        Returns (observation_or_None, poll_buffer).
        The poll_buffer contains EVERY poll response as a dict, ready for
        batch insert. This is accumulated in memory during the burst.
        """
        poll_start = time.monotonic()
        attempt = 0
        total_interval_ms = 0.0
        deadline = expected_drop + timedelta(seconds=DROP_POLL_WINDOW_AFTER_S)
        poll_buffer: list[dict] = []

        try:
            async with ResyApiClient(scout=True, user_id="drop-intel") as client:
                while datetime.now(expected_drop.tzinfo) < deadline and self._running:
                    attempt += 1
                    t0 = time.monotonic()

                    try:
                        slots = await client.find_slots(
                            venue_id, target_date, party_size, fast=True,
                        )
                    except Exception:
                        slots = []

                    # Record EVERY poll in the buffer
                    ms_since_start = (time.monotonic() - poll_start) * 1000
                    entry = {
                        "venue_id": venue_id,
                        "target_date": target_date,
                        "slot_count": len(slots),
                        "ms_since_start": round(ms_since_start, 1),
                        "source": "collector",
                    }
                    if slots:
                        entry["slots_json"] = [
                            {"time": s.date.start, "type": s.config.type}
                            for s in slots[:50]
                        ]
                    poll_buffer.append(entry)

                    if slots:
                        # DROP DETECTED!
                        drop_time = datetime.now(expected_drop.tzinfo)
                        offset_ms = (drop_time - expected_drop).total_seconds() * 1000
                        avg_interval = total_interval_ms / max(attempt - 1, 1)

                        # Mark ms_since_drop on the buffer entry
                        poll_buffer[-1]["ms_since_drop"] = 0

                        # Extract slot metadata
                        slot_types = list({s.config.type for s in slots if s.config.type})
                        slot_times = sorted(s.date.start for s in slots)

                        # Measure Resy clock offset (quick, non-blocking)
                        resy_clock_ms = None
                        try:
                            clock_offset = await client.calibrate_resy_clock(samples=2)
                            resy_clock_ms = round(clock_offset * 1000, 1)
                        except Exception:
                            pass

                        observation = {
                            "venue_id": venue_id,
                            "venue_name": venue_name,
                            "target_date": target_date,
                            "expected_drop_at": expected_drop.isoformat(),
                            "actual_drop_at": drop_time.isoformat(),
                            "offset_ms": round(offset_ms, 1),
                            "slots_found": len(slots),
                            "slot_types": slot_types,
                            "first_slot_time": slot_times[0] if slot_times else None,
                            "last_slot_time": slot_times[-1] if slot_times else None,
                            "poll_attempt": attempt,
                            "poll_interval_ms": round(avg_interval, 1),
                            "resy_clock_offset_ms": resy_clock_ms,
                            "source": "collector",
                        }

                        # Save observation to DB
                        try:
                            await db.insert_drop_observation(observation)
                        except Exception as e:
                            logger.warning("Failed to save drop observation: %s", e)

                        return observation, poll_buffer

                    # Jittered sleep
                    jitter = random.uniform(DROP_POLL_MIN_MS, DROP_POLL_MAX_MS)
                    await asyncio.sleep(jitter / 1000)
                    total_interval_ms += (time.monotonic() - t0) * 1000

        except Exception as e:
            logger.warning("Drop observation failed for venue %d: %s", venue_id, e)

        return None, poll_buffer

    # ------------------------------------------------------------------
    # VELOCITY TRACKING — with early termination
    # ------------------------------------------------------------------

    async def _track_velocity(
        self,
        venue_id: int,
        venue_name: str,
        party_size: int,
        target_date: str,
        observation: dict,
    ) -> None:
        """Take slot snapshots at intervals after drop to measure how fast slots disappear.

        Early termination: stops after 2 consecutive polls return 0 slots.
        """
        try:
            consecutive_empty = 0
            async with ResyApiClient(scout=True, user_id="drop-intel-velocity") as client:
                prev_offset = 0.0
                for offset_s in VELOCITY_SNAPSHOT_OFFSETS_S:
                    if not self._running:
                        break

                    sleep_time = offset_s - prev_offset
                    prev_offset = offset_s
                    await asyncio.sleep(sleep_time)

                    try:
                        slots = await client.find_slots(
                            venue_id, target_date, party_size, fast=True,
                        )
                        await db.insert_slot_snapshot({
                            "venue_id": venue_id,
                            "target_date": target_date,
                            "slot_count": len(slots),
                            "slots_json": [
                                {"time": s.date.start, "type": s.config.type}
                                for s in slots
                            ],
                            "ms_since_drop": offset_s * 1000,
                            "source": "post_drop",
                        })
                        logger.debug(
                            "Velocity snapshot %s +%.2fs: %d slots",
                            venue_name, offset_s, len(slots),
                        )

                        # Early termination
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
            logger.warning("Velocity tracking failed for %d: %s", venue_id, e)

    @staticmethod
    def _next_drop_time(venue: dict) -> datetime | None:
        """Calculate the next drop datetime for a venue."""
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        try:
            tz = ZoneInfo(venue["drop_timezone"])
            now = datetime.now(tz)

            # Today's drop time
            today_drop = now.replace(
                hour=venue["drop_hour"],
                minute=venue["drop_minute"],
                second=0,
                microsecond=0,
            )

            if today_drop > now:
                return today_drop  # Drop hasn't happened yet today

            # Tomorrow's drop
            return today_drop + timedelta(days=1)

        except Exception as e:
            logger.warning("Failed to calculate drop time for venue %d: %s",
                           venue.get("venue_id", 0), e)
            return None


# Singleton
drop_intel_collector = DropIntelCollector()
