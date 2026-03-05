"""Scout System v3 — precision restaurant availability monitoring.

Two scout types:
  Drop Scout         — 5-phase precision polling around drop windows
                       Phase 1: WARM (T-20-30min) — Imperva cookie prewarm
                       Phase 2: VERIFY (T-2-3min) — cookie refresh + Resy clock cal
                       Phase 3: ACTIVATE (T-3-4s) — sync to calibrated clock
                       Phase 4: DROP POLL (T-0+) — ultra-fast 20-50ms polls
                       Phase 5: REPORT — log results, sleep until next day

  Cancellation Scout — burst/pause evasion pattern for per-slot lifecycle
                       BURST: poll every 1-7s for ~30s
                       PAUSE: wait 5-15s
                       REPEAT within configured polling windows

Architecture (singleton pattern):
- ScoutOrchestrator manages all scout tasks
- _refresh_loop() syncs DB → asyncio tasks every 60s
- Per-poll IP rotation via ProxyPool for stealth
- Back-off when active snipes are running
- All timing parameters configurable via scout_settings table
- Error logging to scout_errors table for UI visibility
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
from tablement.web.ip_pool import ip_pool

logger = logging.getLogger(__name__)

# ===== SYSTEM DEFAULTS (fallbacks if settings table not available) =====
DEFAULT_PARTY_SIZE = 2
DEFAULT_DAYS_AHEAD = 30

# Default polling windows by type
DEFAULT_CANCELLATION_WINDOWS = [{"start": "10:00", "end": "22:00"}]

# Snapshot interval
SNAPSHOT_INTERVAL_S = 300  # 5 minutes

# Refresh interval for campaign list
REFRESH_INTERVAL_S = 60

# Error backoff
MAX_ERROR_INTERVAL_S = 300  # Cap at 5 min
ERROR_BACKOFF_MULTIPLIER = 1.5


def _task_key(venue_id: int, campaign_type: str) -> str:
    """Unique key for a scout task (allows drop + cancellation for same venue)."""
    return f"{venue_id}:{campaign_type}"


def compute_drop_polling_windows(drop_hour: int, drop_minute: int) -> list[dict]:
    """Compute display polling window for a drop scout: [drop - 30min, drop + 30min].

    This is used for display on campaign cards. The actual timing is handled
    by the phased approach (warm → verify → activate → poll).
    """
    drop_total = drop_hour * 60 + drop_minute
    start_total = max(0, drop_total - 30)
    end_total = min(24 * 60 - 1, drop_total + 30)

    start_str = f"{start_total // 60:02d}:{start_total % 60:02d}"
    end_str = f"{end_total // 60:02d}:{end_total % 60:02d}"

    return [{"start": start_str, "end": end_str}]


class ScoutOrchestrator:
    """Manages background scout tasks for all configured venues."""

    def __init__(self):
        self._tasks: dict[str, asyncio.Task] = {}  # task_key → task
        self._running = False
        self._app = None
        self._campaigns: dict[str, dict] = {}  # task_key → campaign data
        self._auto_started: set[str] = set()  # task_keys that were auto-started (not manual)

        # Per-scout state (in memory only)
        self._last_seen: dict[str, dict[str, list]] = {}  # task_key → {date: [slots]}
        self._poll_counts: dict[str, int] = {}  # task_key → counter for proxy rotation
        self._last_snapshot: dict[str, float] = {}  # task_key → monotonic time

        # Drop event broadcasting: scout → snipe notification
        self._drop_events: dict[int, asyncio.Event] = {}  # venue_id → Event
        self._drop_slots: dict[int, list] = {}  # venue_id → latest slot data

    async def start(self, app) -> None:
        """Start the scout orchestrator. Call from app lifespan startup."""
        self._running = True
        self._app = app
        logger.info("Scout orchestrator starting...")
        asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        """Stop all scout tasks and await their cancellation."""
        self._running = False

        # Release all IP assignments
        for key in self._tasks:
            try:
                vid = int(key.split(":")[0])
                ip_pool.release(f"scout-drop-{vid}")
                ip_pool.release(f"scout-cancel-{vid}")
            except (ValueError, IndexError):
                pass

        tasks_to_cancel = list(self._tasks.values())
        for task in tasks_to_cancel:
            task.cancel()
        if tasks_to_cancel:
            await asyncio.gather(*tasks_to_cancel, return_exceptions=True)
        self._tasks.clear()
        self._campaigns.clear()
        self._last_seen.clear()
        self._poll_counts.clear()
        self._drop_events.clear()
        self._drop_slots.clear()
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
            "polling_windows": campaign.get("polling_windows", []),
            "target_dates": campaign.get("target_dates", []),
            "total_polls": campaign.get("total_polls", 0),
            "total_events": campaign.get("total_events", 0),
            "last_poll_at": campaign.get("last_poll_at"),
            "last_event_at": campaign.get("last_event_at"),
            "consecutive_errors": campaign.get("consecutive_errors", 0),
            "drop_hour": campaign.get("drop_hour"),
            "drop_minute": campaign.get("drop_minute"),
            "days_ahead": campaign.get("days_ahead", DEFAULT_DAYS_AHEAD),
            "current_phase": campaign.get("current_phase", "idle"),
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
        return statuses

    # ------------------------------------------------------------------
    # Drop event broadcasting (scout → snipe notification)
    # ------------------------------------------------------------------

    def subscribe_drop(self, venue_id: int) -> asyncio.Event | None:
        """Subscribe to drop detection for a venue.

        Returns an asyncio.Event if a drop scout is active for this venue.
        The event is set when the scout detects slots appearing.
        Returns None if no scout is running for this venue.
        """
        key = _task_key(venue_id, "drop")
        if key not in self._tasks or self._tasks[key].done():
            return None
        if venue_id not in self._drop_events:
            self._drop_events[venue_id] = asyncio.Event()
        return self._drop_events[venue_id]

    def get_drop_slots(self, venue_id: int) -> list:
        """Get the slots that triggered the most recent drop event."""
        return self._drop_slots.get(venue_id, [])

    def is_auto_started(self, venue_id: int, campaign_type: str = "drop") -> bool:
        """Check if a scout was auto-started (vs manually started from admin)."""
        return _task_key(venue_id, campaign_type) in self._auto_started

    # ------------------------------------------------------------------
    # Admin controls
    # ------------------------------------------------------------------

    async def start_scout(
        self,
        venue_id: int,
        venue_name: str = "",
        campaign_type: str = "drop",
        auto: bool = False,
        **config,
    ) -> dict:
        """Start a scout for a specific venue. Creates campaign in DB if needed.

        Args:
            auto: If True, this scout was auto-started because 2+ snipes target
                  the same venue. Auto-started scouts are auto-stopped when snipe
                  count drops below 2. Manually started scouts persist.
        """
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
            if campaign_type == "cancellation":
                coro = self._run_cancellation_scout(campaign)
            else:
                coro = self._run_drop_scout(campaign)

            self._tasks[key] = asyncio.create_task(
                coro, name=f"scout-{venue_id}-{campaign_type}",
            )
            if auto:
                self._auto_started.add(key)
            logger.info(
                "Scout started: %s (%d) [%s]%s",
                venue_name or venue_id, venue_id, campaign_type,
                " (auto)" if auto else "",
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
            self._auto_started.discard(key)

        # Release IP assignments for this venue's scouts
        ip_pool.release(f"scout-drop-{venue_id}")
        ip_pool.release(f"scout-cancel-{venue_id}")
        self._drop_events.pop(venue_id, None)
        self._drop_slots.pop(venue_id, None)

        await db.delete_scout_campaign(venue_id, campaign_type)
        logger.info("Scout stopped: %d [%s]", venue_id, campaign_type or "all")

    async def stop_all(self) -> int:
        """Stop all scouts. Returns count stopped."""
        count = len(self._tasks)

        # Release all IP assignments for scouts
        for key in list(self._tasks):
            task = self._tasks.pop(key)
            task.cancel()
            # Extract venue_id from key ("123:drop" → 123)
            try:
                vid = int(key.split(":")[0])
                ip_pool.release(f"scout-drop-{vid}")
                ip_pool.release(f"scout-cancel-{vid}")
            except (ValueError, IndexError):
                pass

        self._campaigns.clear()
        self._last_seen.clear()
        self._poll_counts.clear()
        self._drop_events.clear()
        self._drop_slots.clear()
        self._auto_started.clear()

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
                        if ctype == "cancellation":
                            coro = self._run_cancellation_scout(campaign)
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
                    logger.debug("Scout tables not created yet — run migrations")
                else:
                    logger.warning("Scout refresh failed: %s", e)

            await asyncio.sleep(REFRESH_INTERVAL_S)

    # ------------------------------------------------------------------
    # Drop Scout — 5-phase precision polling
    # ------------------------------------------------------------------

    async def _run_drop_scout(self, campaign: dict) -> None:
        """Phased precision drop scout — runs daily.

        Phase 1: WARM (T-15-25min) — one Imperva prewarm ping
        Phase 2: VERIFY (T-2-3min) — cookie refresh + Resy clock calibration
        Phase 3: ACTIVATE (T-3-4s) — sync to calibrated clock
        Phase 4: DROP POLL (T-0+) — ultra-fast 20-50ms polls w/ persistent client
        Phase 5: REPORT — log results, sleep until next day

        Uses the same timing infrastructure as the sniper:
        - prewarm_imperva() for cookie warming
        - calibrate_resy_clock() for clock sync
        - Persistent client for ultra-fast polling (no new TCP connections)
        """
        venue_id = campaign["venue_id"]
        venue_name = campaign.get("venue_name", f"Venue {venue_id}")
        key = _task_key(venue_id, "drop")
        drop_hour = campaign.get("drop_hour", 9)
        drop_minute = campaign.get("drop_minute", 0)
        days_ahead = campaign.get("days_ahead", DEFAULT_DAYS_AHEAD)

        self._poll_counts.setdefault(key, 0)
        self._last_snapshot.setdefault(key, 0)

        # Assign a dedicated IP for this scout (multi-EIP distribution)
        scout_ip_key = f"scout-drop-{venue_id}"
        local_ip = ip_pool.get_ip(scout_ip_key)

        # Stagger same-time scouts (0-15s spread based on venue_id)
        stagger_s = (venue_id % 10) * 1.5

        logger.info(
            "Drop scout %s (%d): starting — drop=%d:%02d, days_ahead=%d, IP=%s, stagger=%.1fs",
            venue_name, venue_id, drop_hour, drop_minute, days_ahead, local_ip, stagger_s,
        )

        while self._running:
            try:
                settings = await db.get_scout_settings() or {}
                now_et = self._now_et()

                # Calculate today's drop time in ET
                drop_time = now_et.replace(
                    hour=drop_hour, minute=drop_minute, second=0, microsecond=0,
                )
                timeout_minutes = settings.get("drop_timeout_minutes", 5)
                timeout_end = drop_time + timedelta(minutes=timeout_minutes)

                # If today's drop window already passed, sleep until tomorrow
                if now_et > timeout_end:
                    tomorrow_drop = drop_time + timedelta(days=1)
                    warmup_max = settings.get("drop_warmup_max_minutes", 25)
                    next_warmup = tomorrow_drop - timedelta(minutes=warmup_max)
                    sleep_secs = max(0, (next_warmup - now_et).total_seconds())
                    await self._set_phase(venue_id, "sleeping")
                    logger.info(
                        "Drop scout %s: next drop in %.1f hours, sleeping",
                        venue_name, sleep_secs / 3600,
                    )
                    # Sleep in chunks so we respond to cancellation
                    while sleep_secs > 0 and self._running:
                        chunk = min(sleep_secs, 3600)
                        await asyncio.sleep(chunk)
                        sleep_secs -= chunk
                    continue

                # ===== PHASE 1: WARM =====
                warmup_offset = random.uniform(
                    settings.get("drop_warmup_min_minutes", 15),
                    settings.get("drop_warmup_max_minutes", 25),
                )
                warmup_time = drop_time - timedelta(minutes=warmup_offset) + timedelta(seconds=stagger_s)
                verify_max = settings.get("drop_verify_max_minutes", 3)
                verify_boundary = drop_time - timedelta(minutes=verify_max)

                if now_et < warmup_time:
                    # Too early — wait for warmup window
                    wait = (warmup_time - now_et).total_seconds()
                    await self._set_phase(venue_id, "waiting")
                    logger.info("Drop scout %s: Phase 1 in %.0f min", venue_name, wait / 60)
                    await asyncio.sleep(min(wait, 300))
                    continue

                if now_et < verify_boundary:
                    # In warmup window — do ONE Imperva prewarm
                    await self._set_phase(venue_id, "warming")
                    logger.info("Drop scout %s: Phase 1 WARM — prewarming Imperva", venue_name)
                    try:
                        async with ResyApiClient(scout=True, user_id=f"scout-{venue_id}-warm", local_address=local_ip) as client:
                            warm_result = await client.prewarm_imperva()
                            logger.info("Drop scout %s: Imperva warm done — %s", venue_name, warm_result)
                    except Exception as e:
                        logger.warning("Drop scout %s: warm failed: %s", venue_name, e)
                        asyncio.create_task(self._record_error(venue_id, venue_name, "drop", e, 0))

                    # Sleep until verify phase
                    verify_offset = random.uniform(
                        settings.get("drop_verify_min_minutes", 2),
                        float(verify_max),
                    )
                    verify_time = drop_time - timedelta(minutes=verify_offset)
                    wait = max(0, (verify_time - self._now_et()).total_seconds())
                    if wait > 0:
                        await asyncio.sleep(wait)
                    continue

                # ===== PHASE 2: VERIFY + CREATE PERSISTENT CLIENT =====
                activate_offset = random.uniform(
                    float(settings.get("drop_activate_min_seconds", 3)),
                    float(settings.get("drop_activate_max_seconds", 4)),
                )
                activate_time = drop_time - timedelta(seconds=activate_offset)

                if now_et < activate_time:
                    await self._set_phase(venue_id, "verifying")
                    logger.info(
                        "Drop scout %s: Phase 2 VERIFY — cookies + clock calibration",
                        venue_name,
                    )

                    # Create persistent client for phases 2-4 (bound to dedicated IP)
                    scout_user_id = f"scout-{venue_id}-drop-session"
                    client = ResyApiClient(scout=True, user_id=scout_user_id, local_address=local_ip)
                    await client.__aenter__()

                    resy_offset = 0.0
                    try:
                        await client.prewarm_imperva()
                        resy_offset = await client.calibrate_resy_clock(samples=3)
                        if abs(resy_offset) > 0.5:
                            logger.warning(
                                "Drop scout %s: Resy clock offset %.1fms exceeds 500ms — ignoring",
                                venue_name, resy_offset * 1000,
                            )
                            resy_offset = 0.0
                        logger.info(
                            "Drop scout %s: Resy clock offset: %.1fms",
                            venue_name, resy_offset * 1000,
                        )
                    except Exception as e:
                        logger.warning("Drop scout %s: verify failed: %s", venue_name, e)
                        asyncio.create_task(self._record_error(venue_id, venue_name, "drop", e, 0))

                    # Apply clock offset
                    compensated_drop = drop_time - timedelta(seconds=resy_offset)
                    activate_time = compensated_drop - timedelta(seconds=activate_offset)

                    # ===== PHASE 3: ACTIVATE — precision wait =====
                    wait = (activate_time - self._now_et()).total_seconds()
                    if wait > 0:
                        await self._set_phase(venue_id, "syncing")
                        logger.info(
                            "Drop scout %s: Phase 3 ACTIVATE — precision wait %.1fs",
                            venue_name, wait,
                        )
                        if wait > 2:
                            await asyncio.sleep(wait - 2)
                        # Busy-wait for sub-ms precision
                        remaining = max(0, (activate_time - self._now_et()).total_seconds())
                        target_mono = time.monotonic() + remaining
                        while time.monotonic() < target_mono:
                            await asyncio.sleep(0.001)

                    # ===== PHASE 4: DROP POLL — ultra-fast with persistent client =====
                    await self._set_phase(venue_id, "polling")
                    logger.info(
                        "Drop scout %s: Phase 4 DROP POLL — ultra-fast polling started!",
                        venue_name,
                    )

                    target_dates = campaign.get("target_dates", [])
                    if target_dates:
                        # Filter out past dates
                        today_str = date.today().isoformat()
                        target_dates = [d for d in target_dates if d >= today_str]
                    if not target_dates:
                        target_dates = self._get_target_dates(days_ahead)

                    poll_min_ms = settings.get("drop_poll_min_ms", 20)
                    poll_max_ms = settings.get("drop_poll_max_ms", 50)
                    timeout = compensated_drop + timedelta(minutes=timeout_minutes)
                    poll_count = 0
                    party_size = settings.get("default_party_size", DEFAULT_PARTY_SIZE)

                    try:
                        while self._now_et() < timeout and self._running:
                            for target_date in target_dates:
                                if not self._running:
                                    break
                                poll_count += 1
                                interval_ms = random.uniform(poll_min_ms, poll_max_ms)

                                try:
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
                                        key, venue_id, target_date,
                                        len(slots or []), slot_data, now_et,
                                    )

                                    # Broadcast drop to subscribed snipes
                                    if slot_data and venue_id in self._drop_events:
                                        self._drop_slots[venue_id] = slot_data
                                        self._drop_events[venue_id].set()
                                        logger.info(
                                            "Drop scout %s: BROADCAST to subscribed snipes (%d slots)",
                                            venue_name, len(slot_data),
                                        )

                                except Exception as e:
                                    asyncio.create_task(self._record_error(
                                        venue_id, venue_name, "drop", e, poll_count,
                                    ))
                                    # On block: fall back to residential proxy
                                    err_str = str(e).lower()
                                    if "403" in err_str or "429" in err_str:
                                        logger.warning(
                                            "Drop scout %s: IP %s blocked at poll %d, falling back to proxy",
                                            venue_name, local_ip, poll_count,
                                        )
                                        await client.__aexit__(None, None, None)
                                        new_uid = f"scout-{venue_id}-proxy-{poll_count}"
                                        client = ResyApiClient(scout=True, user_id=new_uid)
                                        await client.__aenter__()
                                        await asyncio.sleep(1)

                                self._poll_counts[key] = poll_count
                                await asyncio.sleep(interval_ms / 1000.0)

                    finally:
                        await client.__aexit__(None, None, None)

                    # ===== PHASE 5: REPORT =====
                    await self._set_phase(venue_id, "done")
                    logger.info(
                        "Drop scout %s: Phase 5 REPORT — %d polls completed",
                        venue_name, poll_count,
                    )
                    asyncio.create_task(self._update_stats(venue_id, 0))

                    # Prune memory: clear venue data after daily cycle
                    self._last_seen.pop(key, None)
                    self._poll_counts[key] = 0

                    # Cleanup: drop events + IP assignment
                    self._drop_events.pop(venue_id, None)
                    self._drop_slots.pop(venue_id, None)
                    ip_pool.release(scout_ip_key)

                    # Sleep until next day (loop will recompute)
                    continue

                else:
                    # We're past activate_time but haven't started polling
                    # (e.g., scout just started mid-window) — go straight to polling
                    # without verify/warmup since we missed the window
                    await self._set_phase(venue_id, "polling")
                    logger.info(
                        "Drop scout %s: late start — polling without warmup",
                        venue_name,
                    )

                    target_dates = campaign.get("target_dates", [])
                    if target_dates:
                        today_str = date.today().isoformat()
                        target_dates = [d for d in target_dates if d >= today_str]
                    if not target_dates:
                        target_dates = self._get_target_dates(days_ahead)

                    poll_min_ms = settings.get("drop_poll_min_ms", 20)
                    poll_max_ms = settings.get("drop_poll_max_ms", 50)
                    timeout = drop_time + timedelta(minutes=timeout_minutes)
                    poll_count = 0
                    party_size = settings.get("default_party_size", DEFAULT_PARTY_SIZE)

                    scout_user_id = f"scout-{venue_id}-drop-late"
                    client = ResyApiClient(scout=True, user_id=scout_user_id, local_address=local_ip)
                    await client.__aenter__()

                    try:
                        while self._now_et() < timeout and self._running:
                            for target_date in target_dates:
                                if not self._running:
                                    break
                                poll_count += 1
                                interval_ms = random.uniform(poll_min_ms, poll_max_ms)

                                try:
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
                                        key, venue_id, target_date,
                                        len(slots or []), slot_data, now_et,
                                    )

                                    # Broadcast drop to subscribed snipes
                                    if slot_data and venue_id in self._drop_events:
                                        self._drop_slots[venue_id] = slot_data
                                        self._drop_events[venue_id].set()
                                        logger.info(
                                            "Drop scout %s: BROADCAST to subscribed snipes (%d slots)",
                                            venue_name, len(slot_data),
                                        )

                                except Exception as e:
                                    asyncio.create_task(self._record_error(
                                        venue_id, venue_name, "drop", e, poll_count,
                                    ))
                                    err_str = str(e).lower()
                                    if "403" in err_str or "429" in err_str:
                                        logger.warning(
                                            "Drop scout %s: IP %s blocked at poll %d, falling back to proxy",
                                            venue_name, local_ip, poll_count,
                                        )
                                        await client.__aexit__(None, None, None)
                                        new_uid = f"scout-{venue_id}-late-rotate-{poll_count}"
                                        client = ResyApiClient(scout=True, user_id=new_uid)
                                        await client.__aenter__()
                                        await asyncio.sleep(1)

                                self._poll_counts[key] = poll_count
                                await asyncio.sleep(interval_ms / 1000.0)
                    finally:
                        await client.__aexit__(None, None, None)

                    await self._set_phase(venue_id, "done")
                    asyncio.create_task(self._update_stats(venue_id, 0))

                    # Cleanup: drop events + IP assignment
                    self._drop_events.pop(venue_id, None)
                    self._drop_slots.pop(venue_id, None)
                    ip_pool.release(scout_ip_key)

                    continue

            except asyncio.CancelledError:
                logger.info("Drop scout %s (%d) cancelled", venue_name, venue_id)
                break
            except Exception as e:
                logger.warning("Drop scout %s (%d) loop error: %s", venue_name, venue_id, e)
                await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # Cancellation Scout — burst/pause evasion pattern
    # ------------------------------------------------------------------

    async def _run_cancellation_scout(self, campaign: dict) -> None:
        """Monitor specific dates with per-slot lifecycle tracking.

        Burst/pause evasion pattern:
        - BURST: Poll every 1-7s for ~30 seconds
        - PAUSE: Wait 5-15s
        - REPEAT within configured polling windows

        Tracks when each individual slot appears and disappears, with
        exact timestamps and duration. Records to scout_slot_sightings.
        """
        venue_id = campaign["venue_id"]
        venue_name = campaign.get("venue_name", f"Venue {venue_id}")
        target_dates = campaign.get("target_dates", [])
        campaign_id = campaign.get("id")
        key = _task_key(venue_id, "cancellation")

        self._poll_counts.setdefault(key, 0)

        if not target_dates:
            logger.warning("Cancellation scout %s (%d): no target dates configured", venue_name, venue_id)
            return

        settings = await db.get_scout_settings() or {}
        poll_min = float(settings.get("cancel_poll_min_seconds", 1))
        poll_max = float(settings.get("cancel_poll_max_seconds", 7))
        burst_duration = int(settings.get("cancel_burst_duration_seconds", 30))
        pause_min = float(settings.get("cancel_pause_min_seconds", 5))
        pause_max = float(settings.get("cancel_pause_max_seconds", 15))
        party_size = int(settings.get("default_party_size", DEFAULT_PARTY_SIZE))

        burst_start = time.monotonic()
        settings_reload_counter = 0

        logger.info(
            "Cancellation scout %s (%d): starting — dates=%s, burst=%.0f-%.0fs/%ds, pause=%.0f-%.0fs",
            venue_name, venue_id,
            ", ".join(target_dates[:5]),
            poll_min, poll_max, burst_duration, pause_min, pause_max,
        )

        while self._running:
            try:
                # Check polling window
                if not self._is_within_polling_window(campaign):
                    await asyncio.sleep(60)
                    continue

                if self._should_back_off(venue_id):
                    await asyncio.sleep(30)
                    continue

                # Reload settings periodically (~every 25 min)
                settings_reload_counter += 1
                if settings_reload_counter % 50 == 0:
                    settings = await db.get_scout_settings() or {}
                    poll_min = float(settings.get("cancel_poll_min_seconds", 1))
                    poll_max = float(settings.get("cancel_poll_max_seconds", 7))
                    burst_duration = int(settings.get("cancel_burst_duration_seconds", 30))
                    pause_min = float(settings.get("cancel_pause_min_seconds", 5))
                    pause_max = float(settings.get("cancel_pause_max_seconds", 15))
                    party_size = int(settings.get("default_party_size", DEFAULT_PARTY_SIZE))

                # Check burst/pause cycle
                elapsed = time.monotonic() - burst_start
                if elapsed >= burst_duration:
                    # PAUSE phase
                    pause = random.uniform(pause_min, pause_max)
                    await asyncio.sleep(pause)
                    burst_start = time.monotonic()
                    continue

                # Filter out past dates
                today = date.today().isoformat()
                active_dates = [d for d in target_dates if d >= today]

                # Prune past dates from memory
                if key in self._last_seen:
                    self._last_seen[key] = {
                        d: v for d, v in self._last_seen[key].items() if d >= today
                    }

                if not active_dates:
                    logger.info(
                        "Cancellation scout %s (%d): all target dates have passed, stopping",
                        venue_name, venue_id,
                    )
                    await db.delete_scout_campaign(venue_id, "cancellation")
                    break

                # BURST phase: poll each target date
                for target_date in active_dates:
                    if not self._running:
                        break

                    self._poll_counts[key] = self._poll_counts.get(key, 0) + 1
                    user_id = f"scout-{venue_id}-cancel-{self._poll_counts[key]}"

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

                    except Exception as e:
                        asyncio.create_task(self._record_error(
                            venue_id, venue_name, "cancellation", e,
                            self._poll_counts.get(key, 0),
                        ))

                asyncio.create_task(self._update_stats(venue_id, 0))

                # Randomized burst interval
                interval = random.uniform(poll_min, poll_max)
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                logger.info("Cancellation scout %s (%d) cancelled", venue_name, venue_id)
                break
            except Exception as e:
                logger.warning("Cancellation scout %s (%d) loop error: %s", venue_name, venue_id, e)
                await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # Slot sighting tracking (Cancellation Scout)
    # ------------------------------------------------------------------

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
                    "venue_name": venue_name,
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
                    "venue_name": venue_name,
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
                "Cancellation scout %s: %s slot APPEARED at %s [%s]",
                venue_name, slot_time, now_et.strftime("%I:%M:%S %p"), target_date,
            )

        # 4. Disappeared slots → close sighting + event
        gone = sighting_times - current_times
        for slot_time in gone:
            sighting = sighting_map[slot_time]
            first_seen = sighting.get("first_seen_at", now_iso)

            # Compute duration with ms precision
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
                    duration_seconds=round(duration, 3),  # ms precision
                )
            except Exception as e:
                logger.warning("Failed to close sighting: %s", e)

            # Record event
            try:
                await db.insert_scout_event({
                    "venue_id": venue_id,
                    "venue_name": venue_name,
                    "target_date": target_date,
                    "event_type": "slot_disappeared",
                    "prev_slot_count": len(sighting_times),
                    "new_slot_count": len(current_times),
                    "slots_added": [],
                    "slots_removed": [{"time": slot_time, "duration_s": round(duration, 3)}],
                    "hour_et": now_et.hour,
                    "day_of_week": now_et.weekday(),
                })
            except Exception:
                pass

            # Format duration nicely
            if duration < 1:
                dur_str = f"{duration * 1000:.0f}ms"
            elif duration < 60:
                dur_str = f"{duration:.1f}s"
            else:
                dur_str = f"{duration / 60:.0f}m{duration % 60:.0f}s"

            logger.info(
                "Cancellation scout %s: %s slot GONE at %s (lasted %s) [%s]",
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
            "venue_name": venue_name,
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
                "Scout event: %s %s — %s: %d → %d slots (+%d/-%d) [%s]",
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
    # Phase tracking
    # ------------------------------------------------------------------

    async def _set_phase(self, venue_id: int, phase: str) -> None:
        """Update the current phase for UI display."""
        for key, campaign in self._campaigns.items():
            if campaign.get("venue_id") == venue_id:
                campaign["current_phase"] = phase
                break
        try:
            await db.update_scout_campaign_stats(venue_id, current_phase=phase)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Error recording
    # ------------------------------------------------------------------

    async def _record_error(
        self, venue_id: int, venue_name: str,
        campaign_type: str, error: Exception, poll_count: int,
    ) -> None:
        """Classify and record a scout error to DB for UI visibility."""
        err_str = str(error).lower()
        if "403" in err_str or "forbidden" in err_str:
            error_type = "resy_block"
        elif "429" in err_str or "rate" in err_str:
            error_type = "rate_limit"
        elif "proxy" in err_str or "connect" in err_str:
            error_type = "proxy_error"
        elif "timeout" in err_str:
            error_type = "timeout"
        else:
            error_type = "unknown"

        try:
            await db.insert_scout_error({
                "venue_id": venue_id,
                "venue_name": venue_name,
                "campaign_type": campaign_type,
                "error_type": error_type,
                "error_message": str(error)[:500],
                "poll_count": poll_count,
            })
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

    def _is_within_polling_window(self, campaign: dict) -> bool:
        """Check if current time falls within any configured polling window."""
        windows = campaign.get("polling_windows", [])
        if not windows:
            return True  # No windows configured = always active (fallback)

        now = self._now_et()
        current_minutes = now.hour * 60 + now.minute

        for w in windows:
            try:
                start_parts = w["start"].split(":")
                end_parts = w["end"].split(":")
                start_min = int(start_parts[0]) * 60 + int(start_parts[1])
                end_min = int(end_parts[0]) * 60 + int(end_parts[1])

                # Handle overnight windows (e.g., 23:00 - 01:00)
                if end_min <= start_min:
                    if current_minutes >= start_min or current_minutes < end_min:
                        return True
                else:
                    if start_min <= current_minutes < end_min:
                        return True
            except (KeyError, ValueError, IndexError):
                continue

        return False

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
