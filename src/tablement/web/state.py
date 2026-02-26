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

    @property
    def active_count(self) -> int:
        return len(self._jobs)
