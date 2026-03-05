"""Scout System — continuous restaurant availability monitoring.

Runs 20-30 "scouts" that continuously watch restaurants throughout the day,
tracking when slots appear/disappear, how fast they go, and when cancellations
happen. This data feeds heat maps, velocity charts, and helps optimize snipe
timing for customers.

Architecture (same singleton pattern as DropIntelCollector):
- ScoutOrchestrator manages all scout tasks
- _refresh_loop() syncs DB → asyncio tasks every 60s
- _run_scout() per-restaurant polling loop with change detection
- Per-poll IP rotation via ProxyPool for stealth
- Back-off when active snipes are running
- Drop window deference to DropIntelCollector

Key design:
- Change-only recording: events when availability changes
- Periodic snapshots every 5 min as backup
- Enhanced polling near drop windows (5s instead of 45s)
- Active hours: 9 AM – 1 AM ET
"""

from __future__ import annotations

import asyncio
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


class ScoutOrchestrator:
    """Manages background scout tasks for all configured venues."""

    def __init__(self):
        self._tasks: dict[int, asyncio.Task] = {}  # venue_id → task
        self._running = False
        self._app = None
        self._campaigns: dict[int, dict] = {}  # venue_id → campaign data

        # Per-scout state (in memory only)
        self._last_seen: dict[int, dict[str, list]] = {}  # venue_id → {date: [slots]}
        self._poll_counts: dict[int, int] = {}  # venue_id → counter for proxy rotation
        self._last_snapshot: dict[int, float] = {}  # venue_id → monotonic time

    async def start(self, app) -> None:
        """Start the scout orchestrator. Call from app lifespan startup."""
        self._running = True
        self._app = app
        logger.info("Scout orchestrator starting...")
        asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        """Stop all scout tasks."""
        self._running = False
        for venue_id, task in self._tasks.items():
            task.cancel()
        self._tasks.clear()
        self._campaigns.clear()
        logger.info("Scout orchestrator stopped — %d scouts cancelled", len(self._tasks))

    @property
    def active_scouts(self) -> int:
        """Number of currently running scout tasks."""
        return sum(1 for t in self._tasks.values() if not t.done())

    def get_campaign_status(self, venue_id: int) -> dict | None:
        """Get live status for a scout campaign (for API)."""
        campaign = self._campaigns.get(venue_id)
        if not campaign:
            return None

        task = self._tasks.get(venue_id)
        running = task is not None and not task.done()

        return {
            "venue_id": venue_id,
            "venue_name": campaign.get("venue_name", ""),
            "running": running,
            "poll_count": self._poll_counts.get(venue_id, 0),
            "poll_interval_s": campaign.get("poll_interval_s", 45),
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
        for venue_id, campaign in self._campaigns.items():
            status = self.get_campaign_status(venue_id)
            if status:
                statuses.append(status)
        return sorted(statuses, key=lambda s: s.get("priority", 0), reverse=True)

    # ------------------------------------------------------------------
    # Admin controls
    # ------------------------------------------------------------------

    async def start_scout(self, venue_id: int, venue_name: str = "", **config) -> dict:
        """Start a scout for a specific venue. Creates campaign in DB if needed."""
        campaign_data = {
            "venue_name": venue_name,
            "active": True,
            **config,
        }
        campaign = await db.upsert_scout_campaign(venue_id, **campaign_data)
        self._campaigns[venue_id] = campaign

        # Start the task if not already running
        if venue_id not in self._tasks or self._tasks[venue_id].done():
            self._tasks[venue_id] = asyncio.create_task(
                self._run_scout(campaign),
                name=f"scout-{venue_id}",
            )
            logger.info("Scout started: %s (%d)", venue_name or venue_id, venue_id)

        return campaign

    async def stop_scout(self, venue_id: int) -> None:
        """Stop a specific scout."""
        task = self._tasks.pop(venue_id, None)
        if task:
            task.cancel()
        self._campaigns.pop(venue_id, None)
        self._last_seen.pop(venue_id, None)
        self._poll_counts.pop(venue_id, None)
        await db.delete_scout_campaign(venue_id)
        logger.info("Scout stopped: %d", venue_id)

    async def stop_all(self) -> int:
        """Stop all scouts. Returns count stopped."""
        count = len(self._tasks)
        for venue_id in list(self._tasks):
            await self.stop_scout(venue_id)
        return count

    async def start_all_curated(self) -> int:
        """Start scouts for all active curated restaurants. Returns count started."""
        restaurants = await db.list_curated_restaurants(active_only=True)
        started = 0
        for r in restaurants:
            venue_id = r["venue_id"]
            if venue_id not in self._tasks or self._tasks[venue_id].done():
                await self.start_scout(
                    venue_id=venue_id,
                    venue_name=r.get("name", ""),
                    drop_hour=r.get("drop_hour"),
                    drop_minute=r.get("drop_minute", 0),
                    days_ahead=r.get("drop_days_ahead", 30),
                )
                started += 1
        return started

    # ------------------------------------------------------------------
    # Core loops
    # ------------------------------------------------------------------

    async def _refresh_loop(self) -> None:
        """Every 60s, sync DB campaigns → running tasks."""
        while self._running:
            try:
                campaigns = await db.list_scout_campaigns(active_only=True)
                active_ids = {c["venue_id"] for c in campaigns}

                # Stop tasks for deactivated campaigns
                for vid in list(self._tasks):
                    if vid not in active_ids:
                        task = self._tasks.pop(vid)
                        task.cancel()
                        self._campaigns.pop(vid, None)
                        logger.info("Scout %d removed (deactivated in DB)", vid)

                # Start tasks for new/restarted campaigns
                for campaign in campaigns:
                    vid = campaign["venue_id"]
                    self._campaigns[vid] = campaign
                    if vid not in self._tasks or self._tasks[vid].done():
                        self._tasks[vid] = asyncio.create_task(
                            self._run_scout(campaign),
                            name=f"scout-{vid}",
                        )
                        logger.info(
                            "Scout started via refresh: %s (%d)",
                            campaign.get("venue_name", "?"), vid,
                        )

            except Exception as e:
                # Only log once per error type to avoid spam (e.g., table not created yet)
                err_str = str(e)
                if "PGRST205" in err_str:
                    logger.debug("Scout tables not created yet — run migration 010")
                else:
                    logger.warning("Scout refresh failed: %s", e)

            await asyncio.sleep(REFRESH_INTERVAL_S)

    async def _run_scout(self, campaign: dict) -> None:
        """Per-restaurant polling loop with change detection."""
        venue_id = campaign["venue_id"]
        venue_name = campaign.get("venue_name", f"Venue {venue_id}")
        poll_interval = campaign.get("poll_interval_s", 45)
        enhanced_interval = campaign.get("enhanced_interval_s", 5)
        enhanced_window_min = campaign.get("enhanced_window_min", 10)
        party_size = campaign.get("party_size", 2)
        days_ahead = campaign.get("days_ahead", 30)

        self._poll_counts.setdefault(venue_id, 0)
        self._last_snapshot.setdefault(venue_id, 0)
        error_count = 0

        logger.info(
            "Scout %s (%d): starting — interval=%ds, party=%d, days_ahead=%d",
            venue_name, venue_id, poll_interval, party_size, days_ahead,
        )

        while self._running:
            try:
                # 1. Check active hours (9 AM – 1 AM ET)
                if not self._is_active_hours():
                    await asyncio.sleep(60)
                    continue

                # 2. Back off if there's an active snipe for this venue
                if self._should_back_off(venue_id):
                    logger.debug("Scout %d: backing off (active snipe)", venue_id)
                    await asyncio.sleep(30)
                    continue

                # 3. Check if we're in drop window → yield to DropIntelCollector
                if self._is_in_drop_window(campaign):
                    logger.debug("Scout %d: yielding to drop intel", venue_id)
                    await asyncio.sleep(5)
                    continue

                # 4. Determine polling interval (enhanced near drop?)
                interval = poll_interval
                if self._is_near_drop_window(campaign, enhanced_window_min):
                    interval = enhanced_interval

                # 5. Generate target dates to check
                target_dates = self._get_target_dates(days_ahead)

                # 6. Poll each target date
                for target_date in target_dates:
                    if not self._running:
                        break

                    # Rotate proxy per poll
                    self._poll_counts[venue_id] += 1
                    user_id = f"scout-{venue_id}-{self._poll_counts[venue_id]}"

                    try:
                        async with ResyApiClient(
                            scout=True,
                            user_id=user_id,
                        ) as client:
                            slots = await client.find_slots(
                                venue_id, target_date, party_size, fast=True,
                            )

                        # Process results
                        now_et = self._now_et()
                        slot_data = []
                        if slots:
                            slot_data = [
                                {"time": s.date.start, "type": s.config.type}
                                for s in slots[:50]
                            ]

                        # Diff against last seen
                        await self._process_diff(
                            venue_id, venue_name, target_date,
                            slot_data, len(slots), now_et,
                        )

                        # Periodic snapshot
                        await self._maybe_snapshot(
                            venue_id, target_date, len(slots), slot_data, now_et,
                        )

                        error_count = 0  # Reset on success

                    except Exception as e:
                        error_count += 1
                        if error_count <= 3 or error_count % 10 == 0:
                            logger.warning(
                                "Scout %s (%d) poll error #%d: %s",
                                venue_name, venue_id, error_count, e,
                            )

                # 7. Update campaign stats in DB (fire-and-forget)
                asyncio.create_task(self._update_stats(venue_id, error_count))

                # 8. Sleep with jitter (±20%)
                jitter = interval * random.uniform(-0.2, 0.2)
                effective_interval = max(interval + jitter, 2.0)

                # Error backoff
                if error_count > 0:
                    backoff = min(
                        effective_interval * (ERROR_BACKOFF_MULTIPLIER ** min(error_count, 8)),
                        MAX_ERROR_INTERVAL_S,
                    )
                    effective_interval = max(effective_interval, backoff)
                    # Force proxy rotation on errors
                    proxy_pool.rotate_session(f"scout-{venue_id}-{self._poll_counts[venue_id]}")

                await asyncio.sleep(effective_interval)

            except asyncio.CancelledError:
                logger.info("Scout %s (%d) cancelled", venue_name, venue_id)
                break
            except Exception as e:
                logger.warning("Scout %s (%d) loop error: %s", venue_name, venue_id, e)
                await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    async def _process_diff(
        self,
        venue_id: int,
        venue_name: str,
        target_date: str,
        current_slots: list[dict],
        slot_count: int,
        now_et: datetime,
    ) -> None:
        """Compare current slots against last seen, record changes."""
        key = f"{venue_id}:{target_date}"
        prev_slots = self._last_seen.get(venue_id, {}).get(target_date, [])
        prev_count = len(prev_slots)

        # Extract slot times for comparison
        current_times = {s.get("time", "") for s in current_slots}
        prev_times = {s.get("time", "") for s in prev_slots}

        if current_times == prev_times:
            # No change
            return

        # Something changed!
        added = current_times - prev_times
        removed = prev_times - current_times

        # Determine event type
        if added and not removed:
            event_type = "slots_appeared"
        elif removed and not added:
            event_type = "slots_disappeared"
        else:
            event_type = "slots_changed"

        # Build event
        event = {
            "venue_id": venue_id,
            "target_date": target_date,
            "event_type": event_type,
            "prev_slot_count": prev_count,
            "new_slot_count": slot_count,
            "slots_added": [{"time": t} for t in sorted(added)] if added else [],
            "slots_removed": [{"time": t} for t in sorted(removed)] if removed else [],
            "hour_et": now_et.hour,
            "day_of_week": now_et.weekday(),  # 0=Mon, 6=Sun
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

        # Update last seen
        if venue_id not in self._last_seen:
            self._last_seen[venue_id] = {}
        self._last_seen[venue_id][target_date] = current_slots

    async def _maybe_snapshot(
        self,
        venue_id: int,
        target_date: str,
        slot_count: int,
        slots_json: list[dict],
        now_et: datetime,
    ) -> None:
        """Take a snapshot every SNAPSHOT_INTERVAL_S."""
        now_mono = time.monotonic()
        last = self._last_snapshot.get(venue_id, 0)

        if now_mono - last < SNAPSHOT_INTERVAL_S:
            return

        self._last_snapshot[venue_id] = now_mono

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
            await db.update_scout_campaign_stats(
                venue_id,
                total_polls=self._poll_counts.get(venue_id, 0),
                last_poll_at=datetime.utcnow().isoformat(),
                consecutive_errors=error_count,
            )
        except Exception:
            pass  # Non-critical

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
        # Active from 9 AM (9) to 1 AM next day (active if hour >= 9 or hour < 1)
        return hour >= ACTIVE_HOUR_START or hour < (ACTIVE_HOUR_END - 24)

    def _should_back_off(self, venue_id: int) -> bool:
        """Check if there's an active snipe running for this venue."""
        if not self._app:
            return False

        jobs = getattr(self._app.state, "jobs", None)
        if not jobs:
            return False

        # Check all active jobs for this venue
        for job in jobs.list_all_jobs():
            # The job message or reservation might reference this venue
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

        # Build today's drop time
        drop_time = now.replace(
            hour=drop_hour, minute=drop_minute, second=0, microsecond=0,
        )

        # Check if within T-10s to T+20s
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

        # Within window_minutes before or after drop
        delta = abs((now - drop_time).total_seconds())
        return delta < (window_minutes * 60)

    @staticmethod
    def _get_target_dates(days_ahead: int) -> list[str]:
        """Get 2-3 target dates to check per poll cycle.

        Returns the drop date + a couple nearby interesting dates.
        """
        today = date.today()

        # Primary: the date that's days_ahead from now (drop target)
        drop_date = today + timedelta(days=days_ahead)

        # Secondary: tomorrow (cancellation window)
        tomorrow = today + timedelta(days=1)

        # Tertiary: next weekend day
        days_to_saturday = (5 - today.weekday()) % 7
        if days_to_saturday == 0:
            days_to_saturday = 7
        next_weekend = today + timedelta(days=days_to_saturday)

        # Deduplicate and limit
        dates = list(dict.fromkeys([
            drop_date.isoformat(),
            tomorrow.isoformat(),
            next_weekend.isoformat(),
        ]))

        return dates[:3]


# Singleton
scout_orchestrator = ScoutOrchestrator()
