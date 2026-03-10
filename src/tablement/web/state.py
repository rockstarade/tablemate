"""In-memory state for real-time job tracking and SSE broadcasting.

SnipePhase and SnipeStatus are kept for SSE real-time updates.
JobState tracks per-reservation live state (phase, countdown, SSE queues).
The persistent data lives in Supabase — this module only handles ephemeral
in-flight state that doesn't need to survive restarts.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum

from tablement.models import SnipeResult

logger = logging.getLogger(__name__)


class SnipePhase(str, Enum):
    IDLE = "idle"
    WAITING = "waiting"
    AUTHENTICATING = "authenticating"
    WARMING = "warming"
    SNIPING = "sniping"
    MONITORING = "monitoring"
    DONE = "done"


@dataclass
class SnipeStatus:
    phase: SnipePhase = SnipePhase.IDLE
    attempt: int = 0
    slots_found: int = 0
    message: str = ""
    result: SnipeResult | None = None
    drop_datetime_iso: str | None = None


@dataclass
class JobState:
    """Ephemeral in-memory state for one active reservation job.

    Each running snipe or monitor job gets a JobState. It holds the
    asyncio.Task, the current SnipeStatus, and the SSE subscriber queues
    for that specific reservation.
    """

    reservation_id: str
    user_id: str
    status: SnipeStatus = field(default_factory=SnipeStatus)
    task: asyncio.Task | None = None
    event_queues: list[asyncio.Queue] = field(default_factory=list)

    def broadcast(self, event: str, data: dict) -> None:
        """Push an SSE event to all connected clients watching this job."""
        msg = {"event": event, "data": data}
        for q in self.event_queues:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # Drop if client is slow

    def cancel(self) -> None:
        """Cancel the running task."""
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None


class JobManager:
    """Manages all active reservation jobs (snipe + monitor) in memory.

    Keyed by reservation_id. Jobs are added when a reservation starts
    executing and removed when they complete (or on server restart).
    """

    def __init__(self) -> None:
        self._jobs: dict[str, JobState] = {}

    def create(self, reservation_id: str, user_id: str) -> JobState:
        """Create a new job state for a reservation."""
        # Cancel existing job for this reservation if any
        existing = self._jobs.get(reservation_id)
        if existing:
            existing.cancel()

        job = JobState(reservation_id=reservation_id, user_id=user_id)
        self._jobs[reservation_id] = job
        return job

    def get(self, reservation_id: str) -> JobState | None:
        """Get a job state by reservation ID."""
        return self._jobs.get(reservation_id)

    def remove(self, reservation_id: str) -> None:
        """Remove and cancel a job."""
        job = self._jobs.pop(reservation_id, None)
        if job:
            job.cancel()

    def get_user_jobs(self, user_id: str) -> list[JobState]:
        """Get all active jobs for a user."""
        return [j for j in self._jobs.values() if j.user_id == user_id]

    def cancel_all(self) -> None:
        """Cancel all running jobs (for shutdown)."""
        for job in self._jobs.values():
            job.cancel()
        self._jobs.clear()

    def list_all_jobs(self) -> list[dict]:
        """Return all active jobs with serialized status for admin dashboard."""
        result = []
        for rid, job in self._jobs.items():
            result.append({
                "reservation_id": rid,
                "user_id": job.user_id,
                "phase": job.status.phase.value,
                "attempt": job.status.attempt,
                "slots_found": job.status.slots_found,
                "message": job.status.message,
            })
        return result

    @property
    def active_count(self) -> int:
        return len(self._jobs)


class SnipeQueue:
    """First-come-first-served ordering for competing snipes.

    Keyed by (venue_id, target_date). When multiple users target the
    same restaurant on the same date, this determines who fires first
    based on reservation created_at timestamp.

    NOT per time slot — because each user can only hold ONE reservation
    per restaurant (Resy constraint), so the competition is for ANY
    table at that venue, not a specific time.

    Stagger approach: lower-priority users are DELAYED, not blocked.
    If User 1 grabs the token, User 2's booking fails naturally and
    the retry loop re-polls for fresh slots. User 2 is never marked
    as failed — they always get to attempt.
    """

    STAGGER_MS = 150  # delay per queue position

    def __init__(self) -> None:
        self._queues: dict[tuple[int, str], list[dict]] = {}
        # (venue_id, target_date) → sorted list of entries

    def register(
        self,
        venue_id: int,
        target_date: str,
        reservation_id: str,
        user_id: str,
        created_at: str,
    ) -> None:
        """Add a snipe to the queue. Sorted by created_at (FCFS)."""
        key = (venue_id, target_date)
        if key not in self._queues:
            self._queues[key] = []
        existing = {e["reservation_id"] for e in self._queues[key]}
        if reservation_id not in existing:
            self._queues[key].append({
                "reservation_id": reservation_id,
                "user_id": user_id,
                "created_at": created_at,
            })
            self._queues[key].sort(key=lambda e: e["created_at"])

    def get_position(
        self, venue_id: int, target_date: str, reservation_id: str,
    ) -> int:
        """0-indexed queue position. 0 = fires first."""
        for i, e in enumerate(self._queues.get((venue_id, target_date), [])):
            if e["reservation_id"] == reservation_id:
                return i
        return 0

    def get_stagger_seconds(
        self, venue_id: int, target_date: str, reservation_id: str,
    ) -> float:
        """Delay in seconds before this snipe should fire."""
        pos = self.get_position(venue_id, target_date, reservation_id)
        return pos * (self.STAGGER_MS / 1000)

    def queue_size(self, venue_id: int, target_date: str) -> int:
        """Number of snipes competing for this venue+date."""
        return len(self._queues.get((venue_id, target_date), []))

    def unregister(
        self, venue_id: int, target_date: str, reservation_id: str,
    ) -> None:
        """Remove on cancel/complete/fail."""
        key = (venue_id, target_date)
        entries = self._queues.get(key, [])
        self._queues[key] = [
            e for e in entries if e["reservation_id"] != reservation_id
        ]
        if not self._queues.get(key):
            self._queues.pop(key, None)

    def get_all_queues(self) -> list[dict]:
        """Return all active queues for admin dashboard."""
        result = []
        for (venue_id, target_date), entries in self._queues.items():
            result.append({
                "venue_id": venue_id,
                "target_date": target_date,
                "size": len(entries),
                "entries": [
                    {
                        "reservation_id": e["reservation_id"],
                        "user_id": e["user_id"],
                        "position": i,
                        "stagger_ms": i * self.STAGGER_MS,
                    }
                    for i, e in enumerate(entries)
                ],
            })
        return result
