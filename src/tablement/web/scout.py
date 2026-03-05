"""Scout System — continuous restaurant availability monitoring.

Two scout types:
  Drop Scout  — monitors a restaurant around its known drop window
  Date Scout  — monitors specific dates, tracks per-slot lifecycle
               ("5:30 PM appeared at 5:32:14 PM, gone 28s later")

Architecture (singleton pattern, same as DropIntelCollector):
- ScoutOrchestrator manages all scout tasks
- _refresh_loop() syncs DB → asyncio tasks every 60s
- _run_drop_scout() — existing change-detection polling (bulk events)
- _run_date_scout() — per-slot lifecycle tracking (sightings table)
- Per-poll IP rotation via ProxyPool for stealth
- Back-off when active snipes are running
- Drop window deference to DropIntelCollector

Key design:
- Drop Scout: change-only recording via _process_diff
- Date Scout: per-slot sightings with appear/disappear timestamps
- Periodic snapshots every 5 min as backup
- Enhanced polling near drop windows (5s instead of 45s)
- Active hours: 9 AM – 1 AM ET
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timedelta, date

from tablement.api import ResyApiClient
from tablement.proxy import proxy_pool
from tablement.web import db

logger = logging.getLogger(__name__)

# Active hours in ET (9 AM to 1 AM next day)
ACTIVE_HOUR_START = 9
ACTIVE_HOUR_END = 25  # 1 AM = 25 (24 + 1)

# Snapshot interval
SNAPSHOT_INTERVAL_S = 300  # 5 minutes

# Refresh interval for campaign list
REFRESH_INTERVAL_S = 60

# Drop window deference (don't poll during DropIntelCollector's burst)
DROP_WINDOW_BEFORE_S = 10
DROP_WINDOW_AFTER_S = 20

# Error backoff
MAX_ERROR_INTERVAL_S = 300  # Cap at 5 min
ERROR_BACKOFF_MULTIPLIER = 1.5


def _task_key(venue_id: int, campaign_type: str) -> str:
    """Unique key for a scout task (allows drop + date for same venue)."""
    return f"{venue_id}:{campaign_type}"


class ScoutOrchestrator:
    """Manages background scout tasks for all configured venues."""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}  # task_key → task
        self._running = False
        self._app = None
        self._campaigns: dict[str, dict] = {}  # task_key → campaign data

        # Per-scout state (in memory only)
        self._last_seen: dict[str, dict[str, list]] = {}  # task_key → {date: [slots]}
        self._poll_counts: dict[str, int] = {}  # task_key → counter for proxy rotation
        self._last_snapshot: dict[str, float] = {}  # task_key → monotonic time

    async def start(self, app) -> None:
        """Start the scout orchestrator. Call from app lifespan startup."""
        self._running = True
        self._app = app
        logger.info("Scout orchestrator starting...")
        asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        """Stop all scout tasks."""
        self._running = False
        for key, task in self._tasks.items():
            task.cancel()
        self._tasks.clear()
        self._campaigns.clear()
        logger.info("Scout orchestrator stopped")

    @property
    def active_scouts(self) -> int:
        """Number of currently running scout tasks."""
        return sum(1 for t in self._tasks.values() if not t.done())

    def get_campaign_status(self, venue_id: int, campaign_type: str = "drop") -> dict | None:
        """Get live status for a scout campaign (for API)."""
        key = _task_key(venue_id, campaign_type)
        campaign = self._campaigns.get(key)
        if not campaign:
            return None

        task = self._tasks.get(key)
        running = task is not None and not task.done()

        return {
            "venue_id": venue_id,
            "venue_name": campaign.get("venue_name", ""),
            "type": campaign.get("type", "drop"),
            "running": running,
            "poll_count": self._poll_counts.get(key, 0),
            "poll_interval_s": campaign.get("poll_interval_s", 45),
            "target_dates": campaign.get("target_dates", []),
            "total_polls": campaign.get("total_polls", 0),
            "total_events": campaign.get("total_events", 0),
            "last_poll_at": campaign.get("last_poll_at"),
            "last_event_at": campaign.get("last_event_at"),
            "consecutive_errors": campaign.get("consecutive_errors", 0),
            "priority": campaign.get("priority", 0),
        }

    def get_all_statuses(self) -> list[dict]:
        """Get live status for all campaigns."""
        statuses = []
        for key, campaign in self._campaigns.items():
            vid = campaign.get("venue_id")
            ctype = campaign.get("type", "drop")
            status = self.get_campaign_status(vid, ctype)
            if status:
                statuses.append(status)
        return sorted(statuses, key=lambda s: s.get("priority", 0), reverse=True)

    # ------------------------------------------------------------------
    # Admin controls
    # ------------------------------------------------------------------

    async def start_scout(
        self,
        venue_id: int,
        venue_name: str = "",
        campaign_type: str = "drop",
        **config,
    ) -> dict:
        """Start a scout for a specific venue. Creates campaign in DB if needed."""
        campaign_data = {
            "venue_name": venue_name,
            "active": True,
            **config,
        }
        campaign = await db.upsert_scout_campaign(venue_id, campaign_type=campaign_type, **campaign_data)
        key = _task_key(venue_id, campaign_type)
        self._campaigns[key] = campaign

        # Start the task if not already running
        if key not in self._tasks or self._tasks[key].done():
            if campaign_type == "date":
                coro = self._run_date_scout(campaign)
            else:
                coro = self._run_drop_scout(campaign)

            self._tasks[key] = asyncio.create_task(
                coro, name=f"scout-{venue_id}-{campaign_type}",
            )
            logger.info(
                "Scout started: %s (%d) [%s]",
                venue_name or venue_id, venue_id, campaign_type,
            )

        return campaign

    async def stop_scout(self, venue_id: int, campaign_type: str | None = None) -> None:
        """Stop a specific scout. If campaign_type is None, stops all types for this venue."""
        keys_to_stop = []
        if campaign_type:
            keys_to_stop.append(_task_key(venue_id, campaign_type))
        else:
            for key in list(self._tasks):
                if key.startswith(f"{venue_id}:"):
                    keys_to_stop.append(key)

        for key in keys_to_stop:
            task = self._tasks.pop(key, None)
            if task:
                task.cancel()
            self._campaigns.pop(key, None)
            self._last_seen.pop(key, None)
            self._poll_counts.pop(key, None)

        await db.delete_scout_campaign(venue_id, campaign_type)
        logger.info("Scout stopped: %d [%s]", venue_id, campaign_type or "all")

    async def stop_all(self) -> int:
        """Stop all scouts. Returns count stopped."""
        count = len(self._tasks)
        for key in list(self._tasks):
            task = self._tasks.pop(key)
            task.cancel()
        self._campaigns.clear()
        self._last_seen.clear()
        self._poll_counts.clear()

        # Deactivate all in DB
        try:
            campaigns = await db.list_scout_campaigns(active_only=True)
            for c in campaigns:
                await db.delete_scout_campaign(c["venue_id"], c.get("type"))
        except Exception:
            pass
        return count

    # ------------------------------------------------------------------
    # Core loops
    # ------------------------------------------------------------------

    async def _refresh_loop(self) -> None:
        """Every 60s, sync DB campaigns → running tasks."""
        while self._running:
            try:
                campaigns = await db.list_scout_campaigns(active_only=True)
                active_keys = set()

                for campaign in campaigns:
                    vid = campaign["venue_id"]
                    ctype = campaign.get("type", "drop")
                    key = _task_key(vid, ctype)
                    active_keys.add(key)
                    self._campaigns[key] = campaign

                    if key not in self._tasks or self._tasks[key].done():
                        if ctype == "date":
                            coro = self._run_date_scout(campaign)
                        else:
                            coro = self._run_drop_scout(campaign)

                        self._tasks[key] = asyncio.create_task(
                            coro, name=f"scout-{vid}-{ctype}",
                        )
                        logger.info(
                            "Scout started via refresh: %s (%d) [%s]",
                            campaign.get("venue_name", "?"), vid, ctype,
                        )

                # Stop tasks for deactivated campaigns
                for key in list(self._tasks):
                    if key not in active_keys:
                        task = self._tasks.pop(key)
                        task.cancel()
                        self._campaigns.pop(key, None)
                        logger.info("Scout %s removed (deactivated in DB)", key)

            except Exception as e:
                err_str = str(e)
                if "PGRST205" in err_str:
                    logger.debug("Scout tables not created yet — run migration 010/011")
                else:
                    logger.warning("Scout refresh failed: %s", e)

            await asyncio.sleep(REFRESH_INTERVAL_S)

    # ------------------------------------------------------------------
    # Drop Scout — bulk change detection (existing logic)
    # ------------------------------------------------------------------

    async def _run_drop_scout(self, campaign: dict) -> None:
        """Per-restaurant polling loop with bulk change detection."""
        venue_id = campaign["venue_id"]
        venue_name = campaign.get("venue_name", f"Venue {venue_id}")
        poll_interval = campaign.get("poll_interval_s", 45)
        enhanced_interval = campaign.get("enhanced_interval_s", 5)
        enhanced_window_min = campaign.get("enhanced_window_min", 10)
        party_size = campaign.get("party_size", 2)
        days_ahead = campaign.get("days_ahead", 30)
        key = _task_key(venue_id, "drop")

        self._poll_counts.setdefault(key, 0)
        self._last_snapshot.setdefault(key, 0)
        error_count = 0

        logger.info(
            "Drop scout %s (%d): starting — interval=%ds, party=%d, days_ahead=%d",
            venue_name, venue_id, poll_interval, party_size, days_ahead,
        )

        while self._running:
            try:
                if not self._is_active_hours():
                    await asyncio.sleep(60)
                    continue

                if self._should_back_off(venue_id):
                    await asyncio.sleep(30)
                    continue

                if self._is_in_drop_window(campaign):
                    await asyncio.sleep(5)
                    continue

                interval = poll_interval
                if self._is_near_drop_window(campaign, enhanced_window_min):
                    interval = enhanced_interval

                target_dates = self._get_target_dates(days_ahead)

                for target_date in target_dates:
                    if not self._running:
                        break

                    self._poll_counts[key] = self._poll_counts.get(key, 0) + 1
                    user_id = f"scout-{venue_id}-{self._poll_counts[key]}"

                    try:
                        async with ResyApiClient(scout=True, user_id=user_id) as client:
                            slots = await client.find_slots(
                                venue_id, target_date, party_size, fast=True,
                            )

                        now_et = self._now_et()
                        slot_data = [
                            {"time": s.date.start, "type": s.config.type}
                            for s in (slots or [])[:50]
                        ]

                        await self._process_diff(
                            key, venue_id, venue_name, target_date,
                            slot_data, len(slots or []), now_et,
                        )

                        await self._maybe_snapshot(
                            key, venue_id, target_date, len(slots or []), slot_data, now_et,
                        )

                        error_count = 0

                    except Exception as e:
                        error_count += 1
                        if error_count <= 3 or error_count % 10 == 0:
                            logger.warning(
                                "Drop scout %s (%d) poll error #%d: %s",
                                venue_name, venue_id, error_count, e,
                            )

                asyncio.create_task(self._update_stats(venue_id, error_count))

                jitter = interval * random.uniform(-0.2, 0.2)
                effective_interval = max(interval + jitter, 2.0)

                if error_count > 0:
                    backoff = min(
                        effective_interval * (ERROR_BACKOFF_MULTIPLIER ** min(error_count, 8)),
                        MAX_ERROR_INTERVAL_S,
                    )
                    effective_interval = max(effective_interval, backoff)
                    proxy_pool.rotate_session(f"scout-{venue_id}-{self._poll_counts.get(key, 0)}")

                await asyncio.sleep(effective_interval)

            except asyncio.CancelledError:
                logger.info("Drop scout %s (%d) cancelled", venue_name, venue_id)
                break
            except Exception as e:
                logger.warning("Drop scout %s (%d) loop error: %s", venue_name, venue_id, e)
                await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # Date Scout — per-slot lifecycle tracking
    # ------------------------------------------------------------------

    async def _run_date_scout(self, campaign: dict) -> None:
        """Monitor specific dates with per-slot lifecycle tracking.

        Tracks when each individual slot appears and disappears, with
        exact timestamps and duration. Records to scout_slot_sightings.
        """
        venue_id = campaign["venue_id"]
        venue_name = campaign.get("venue_name", f"Venue {venue_id}")
        poll_interval = campaign.get("poll_interval_s", 45)
        party_size = campaign.get("party_size", 2)
        target_dates = campaign.get("target_dates", [])
        campaign_id = campaign.get("id")
        key = _task_key(venue_id, "date")

        self._poll_counts.setdefault(key, 0)
        error_count = 0

        if not target_dates:
            logger.warning("Date scout %s (%d): no target dates configured", venue_name, venue_id)
            return

        logger.info(
            "Date scout %s (%d): starting — interval=%ds, party=%d, dates=%s",
            venue_name, venue_id, poll_interval, party_size,
            ", ".join(target_dates[:5]),
        )

        while self._running:
            try:
                if not self._is_active_hours():
                    await asyncio.sleep(60)
                    continue

                if self._should_back_off(venue_id):
                    await asyncio.sleep(30)
                    continue

                # Filter out past dates
                today = date.today().isoformat()
                active_dates = [d for d in target_dates if d >= today]

                if not active_dates:
                    logger.info(
                        "Date scout %s (%d): all target dates have passed, stopping",
                        venue_name, venue_id,
                    )
                    await db.delete_scout_campaign(venue_id, "date")
                    break

                for target_date in active_dates:
                    if not self._running:
                        break

                    self._poll_counts[key] = self._poll_counts.get(key, 0) + 1
                    user_id = f"scout-{venue_id}-date-{self._poll_counts[key]}"

                    try:
                        async with ResyApiClient(scout=True, user_id=user_id) as client:
                            slots = await client.find_slots(
                                venue_id, target_date, party_size, fast=True,
                            )

                        now_et = self._now_et()
                        slot_data = [
                            {"time": s.date.start, "type": s.config.type}
                            for s in (slots or [])[:50]
                        ]

                        # Per-slot lifecycle tracking
                        await self._track_slot_sightings(
                            venue_id, venue_name, target_date,
                            slot_data, campaign_id, now_et,
                        )

                        error_count = 0

                    except Exception as e:
                        error_count += 1
                        if error_count <= 3 or error_count % 10 == 0:
                            logger.warning(
                                "Date scout %s (%d) poll error #%d [%s]: %s",
                                venue_name, venue_id, error_count, target_date, e,
                            )

                asyncio.create_task(self._update_stats(venue_id, error_count))

                jitter = poll_interval * random.uniform(-0.2, 0.2)
                effective_interval = max(poll_interval + jitter, 2.0)

                if error_count > 0:
                    backoff = min(
                        effective_interval * (ERROR_BACKOFF_MULTIPLIER ** min(error_count, 8)),
                        MAX_ERROR_INTERVAL_S,
                    )
                    effective_interval = max(effective_interval, backoff)
                    proxy_pool.rotate_session(f"scout-{venue_id}-date-{self._poll_counts.get(key, 0)}")

                await asyncio.sleep(effective_interval)

            except asyncio.CancelledError:
                logger.info("Date scout %s (%d) cancelled", venue_name, venue_id)
                break
            except Exception as e:
                logger.warning("Date scout %s (%d) loop error: %s", venue_name, venue_id, e)
                await asyncio.sleep(60)

    async def _track_slot_sightings(
        self,
        venue_id: int,
        venue_name: str,
        target_date: str,
        current_slots: list[dict],
        campaign_id: str | None,
        now_et: datetime,
    ) -> None:
        """Track individual slot lifecycles (appear/disappear with timestamps).

        For each poll:
        1. Get currently "active" sightings (gone_at IS NULL) for this venue+date
        2. Slots still present → update last_seen_at, increment poll_count
        3. Slots newly appeared → insert new sighting + scout_event
        4. Slots disappeared → set gone_at, compute duration + scout_event
        """
        now_iso = now_et.isoformat()

        # 1. Get active sightings from DB
        try:
            active_sightings = await db.get_active_sightings(venue_id, target_date)
        except Exception as e:
            if "PGRST205" in str(e):
                return  # Table doesn't exist yet
            raise

        # Build lookup: slot_time → sighting
        sighting_map = {}
        for s in active_sightings:
            sighting_map[s["slot_time"]] = s

        current_times = {s["time"] for s in current_slots}
        sighting_times = set(sighting_map.keys())

        # 2. Slots still present → update last_seen_at
        still_here = current_times & sighting_times
        for slot_time in still_here:
            sighting = sighting_map[slot_time]
            try:
                await db.update_sighting(
                    sighting["id"],
                    last_seen_at=now_iso,
                    poll_count=sighting.get("poll_count", 1) + 1,
                )
            except Exception:
                pass  # Non-critical

        # 3. Newly appeared slots → insert sighting + event
        appeared = current_times - sighting_times
        for slot_time in appeared:
            # Find the slot type
            slot_type = ""
            for s in current_slots:
                if s["time"] == slot_time:
                    slot_type = s.get("type", "")
                    break

            try:
                await db.upsert_sighting({
                    "venue_id": venue_id,
                    "target_date": target_date,
                    "slot_time": slot_time,
                    "slot_type": slot_type,
                    "first_seen_at": now_iso,
                    "last_seen_at": now_iso,
                    "campaign_id": campaign_id,
                    "poll_count": 1,
                })
            except Exception as e:
                logger.warning("Failed to insert sighting: %s", e)

            # Record event
            try:
                await db.insert_scout_event({
                    "venue_id": venue_id,
                    "target_date": target_date,
                    "event_type": "slot_appeared",
                    "prev_slot_count": len(sighting_times),
                    "new_slot_count": len(current_times),
                    "slots_added": [{"time": slot_time, "type": slot_type}],
                    "slots_removed": [],
                    "hour_et": now_et.hour,
                    "day_of_week": now_et.weekday(),
                })
            except Exception:
                pass

            logger.info(
                "Date scout %s: %s slot APPEARED at %s [%s]",
                venue_name, slot_time, now_et.strftime("%I:%M:%S %p"), target_date,
            )

        # 4. Disappeared slots → close sighting + event
        gone = sighting_times - current_times
        for slot_time in gone:
            sighting = sighting_map[slot_time]
            first_seen = sighting.get("first_seen_at", now_iso)

            # Compute duration
            try:
                if isinstance(first_seen, str):
                    fs = datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
                else:
                    fs = first_seen
                duration = (now_et - fs).total_seconds()
            except Exception:
                duration = 0

            try:
                await db.close_sighting(
                    sighting["id"],
                    gone_at=now_iso,
                    duration_seconds=round(duration, 1),
                )
            except Exception as e:
                logger.warning("Failed to close sighting: %s", e)

            # Record event
            try:
                await db.insert_scout_event({
                    "venue_id": venue_id,
                    "target_date": target_date,
                    "event_type": "slot_disappeared",
                    "prev_slot_count": len(sighting_times),
                    "new_slot_count": len(current_times),
                    "slots_added": [],
                    "slots_removed": [{"time": slot_time, "duration_s": round(duration, 1)}],
                    "hour_et": now_et.hour,
                    "day_of_week": now_et.weekday(),
                })
            except Exception:
                pass

            # Format duration nicely
            if duration < 60:
                dur_str = f"{duration:.0f}s"
            else:
                dur_str = f"{duration / 60:.0f}m{duration % 60:.0f}s"

            logger.info(
                "Date scout %s: %s slot GONE at %s (lasted %s) [%s]",
                venue_name, slot_time, now_et.strftime("%I:%M:%S %p"),
                dur_str, target_date,
            )

    # ------------------------------------------------------------------
    # Change detection (Drop Scout)
    # ------------------------------------------------------------------

    async def _process_diff(
        self,
        key: str,
        venue_id: int,
        venue_name: str,
        target_date: str,
        current_slots: list[dict],
        slot_count: int,
        now_et: datetime,
    ) -> None:
        """Compare current slots against last seen, record changes."""
        prev_slots = self._last_seen.get(key, {}).get(target_date, [])
        prev_count = len(prev_slots)

        current_times = {s.get("time", "") for s in current_slots}
        prev_times = {s.get("time", "") for s in prev_slots}

        if current_times == prev_times:
            return

        added = current_times - prev_times
        removed = prev_times - current_times

        if added and not removed:
            event_type = "slots_appeared"
        elif removed and not added:
            event_type = "slots_disappeared"
        else:
            event_type = "slots_changed"

        event = {
            "venue_id": venue_id,
            "target_date": target_date,
            "event_type": event_type,
            "prev_slot_count": prev_count,
            "new_slot_count": slot_count,
            "slots_added": [{"time": t} for t in sorted(added)] if added else [],
            "slots_removed": [{"time": t} for t in sorted(removed)] if removed else [],
            "hour_et": now_et.hour,
            "day_of_week": now_et.weekday(),
        }

        try:
            await db.insert_scout_event(event)
            logger.info(
                "Scout event: %s %s — %s: %d → %d slots (±%d/%d) [%s]",
                venue_name, target_date, event_type,
                prev_count, slot_count,
                len(added), len(removed),
                ", ".join(sorted(added)[:3]) if added else "-",
            )
        except Exception as e:
            logger.warning("Failed to insert scout event: %s", e)

        if key not in self._last_seen:
            self._last_seen[key] = {}
        self._last_seen[key][target_date] = current_slots

    async def _maybe_snapshot(
        self,
        key: str,
        venue_id: int,
        target_date: str,
        slot_count: int,
        slots_json: list[dict],
        now_et: datetime,
    ) -> None:
        """Take a snapshot every SNAPSHOT_INTERVAL_S."""
        now_mono = time.monotonic()
        last = self._last_snapshot.get(key, 0)

        if now_mono - last < SNAPSHOT_INTERVAL_S:
            return

        self._last_snapshot[key] = now_mono

        try:
            await db.insert_scout_snapshot({
                "venue_id": venue_id,
                "target_date": target_date,
                "slot_count": slot_count,
                "slots_json": slots_json,
                "hour_et": now_et.hour,
                "day_of_week": now_et.weekday(),
            })
        except Exception as e:
            logger.warning("Scout snapshot insert failed: %s", e)

    async def _update_stats(self, venue_id: int, error_count: int) -> None:
        """Fire-and-forget stats update to DB."""
        try:
            key_prefix = f"{venue_id}:"
            total = sum(v for k, v in self._poll_counts.items() if k.startswith(key_prefix))
            await db.update_scout_campaign_stats(
                venue_id,
                total_polls=total,
                last_poll_at=datetime.utcnow().isoformat(),
                consecutive_errors=error_count,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now_et() -> datetime:
        """Get current time in US/Eastern."""
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))

    def _is_active_hours(self) -> bool:
        """Check if we're within active polling hours (9 AM – 1 AM ET)."""
        now = self._now_et()
        hour = now.hour
        return hour >= ACTIVE_HOUR_START or hour < (ACTIVE_HOUR_END - 24)

    def _should_back_off(self, venue_id: int) -> bool:
        """Check if there's an active snipe running for this venue."""
        if not self._app:
            return False

        jobs = getattr(self._app.state, "jobs", None)
        if not jobs:
            return False

        for job in jobs.list_all_jobs():
            if str(venue_id) in job.get("message", ""):
                return True

        return False

    @staticmethod
    def _is_in_drop_window(campaign: dict) -> bool:
        """Check if we're currently in the drop intel collection window."""
        drop_hour = campaign.get("drop_hour")
        drop_minute = campaign.get("drop_minute", 0)

        if drop_hour is None:
            return False

        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        tz_name = campaign.get("drop_timezone", "America/New_York")
        now = datetime.now(ZoneInfo(tz_name))

        drop_time = now.replace(
            hour=drop_hour, minute=drop_minute, second=0, microsecond=0,
        )

        before = drop_time - timedelta(seconds=DROP_WINDOW_BEFORE_S)
        after = drop_time + timedelta(seconds=DROP_WINDOW_AFTER_S)

        return before <= now <= after

    @staticmethod
    def _is_near_drop_window(campaign: dict, window_minutes: int) -> bool:
        """Check if we're near (but not in) the drop window → use enhanced polling."""
        drop_hour = campaign.get("drop_hour")
        drop_minute = campaign.get("drop_minute", 0)

        if drop_hour is None:
            return False

        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo

        tz_name = campaign.get("drop_timezone", "America/New_York")
        now = datetime.now(ZoneInfo(tz_name))

        drop_time = now.replace(
            hour=drop_hour, minute=drop_minute, second=0, microsecond=0,
        )

        delta = abs((now - drop_time).total_seconds())
        return delta < (window_minutes * 60)

    @staticmethod
    def _get_target_dates(days_ahead: int) -> list[str]:
        """Get 2-3 target dates to check per poll cycle."""
        today = date.today()

        drop_date = today + timedelta(days=days_ahead)
        tomorrow = today + timedelta(days=1)

        days_to_saturday = (5 - today.weekday()) % 7
        if days_to_saturday == 0:
            days_to_saturday = 7
        next_weekend = today + timedelta(days=days_to_saturday)

        dates = list(dict.fromkeys([
            drop_date.isoformat(),
            tomorrow.isoformat(),
            next_weekend.isoformat(),
        ]))

        return dates[:3]


# Singleton
scout_orchestrator = ScoutOrchestrator()
