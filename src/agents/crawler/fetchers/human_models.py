"""Data models and in-memory job queue for the human-assisted fetcher."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class FetchJobStatus(str, Enum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class JobContext:
    """Agent decision context shown to the human operator."""

    university_name: str = ""
    agent_state: str = ""
    intent: str = ""
    parent_url: str = ""
    depth: int = 0
    org_unit_name: str = ""
    hints: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "university_name": self.university_name,
            "agent_state": self.agent_state,
            "intent": self.intent,
            "parent_url": self.parent_url,
            "depth": self.depth,
            "org_unit_name": self.org_unit_name,
            "hints": self.hints,
        }


@dataclass
class FetchJob:
    """A single fetch request waiting for human completion."""

    url: str
    context: JobContext
    timeout_seconds: float = 300.0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: FetchJobStatus = FetchJobStatus.PENDING
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    assigned_at: datetime | None = None
    completed_at: datetime | None = None
    # Result fields (populated on completion)
    result_html: str | None = None
    result_url: str | None = None
    result_title: str | None = None
    error_message: str | None = None
    # Async coordination
    done_event: asyncio.Event = field(default_factory=asyncio.Event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "status": self.status.value,
            "context": self.context.to_dict(),
            "created_at": self.created_at.isoformat(),
            "timeout_seconds": self.timeout_seconds,
        }


class JobQueue:
    """In-memory job queue. Lifecycle matches the agent run."""

    def __init__(self) -> None:
        self._pending: asyncio.Queue[FetchJob] = asyncio.Queue()
        self._jobs: dict[str, FetchJob] = {}

    async def submit(self, job: FetchJob) -> None:
        self._jobs[job.id] = job
        await self._pending.put(job)

    async def next(self, timeout: float = 0) -> FetchJob | None:
        """Return the next pending job, or None if queue is empty."""
        try:
            job = await asyncio.wait_for(self._pending.get(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            return None
        job.status = FetchJobStatus.ASSIGNED
        job.assigned_at = datetime.now(timezone.utc)
        return job

    def get(self, job_id: str) -> FetchJob | None:
        return self._jobs.get(job_id)

    def complete(self, job_id: str, *, html: str, url: str | None = None, title: str | None = None) -> FetchJob:
        job = self._require(job_id)
        job.status = FetchJobStatus.COMPLETED
        job.completed_at = datetime.now(timezone.utc)
        job.result_html = html
        job.result_url = url or job.url
        job.result_title = title
        job.done_event.set()
        return job

    def fail(self, job_id: str, message: str = "") -> FetchJob:
        job = self._require(job_id)
        job.status = FetchJobStatus.FAILED
        job.completed_at = datetime.now(timezone.utc)
        job.error_message = message
        job.done_event.set()
        return job

    def skip(self, job_id: str) -> FetchJob:
        job = self._require(job_id)
        job.status = FetchJobStatus.SKIPPED
        job.completed_at = datetime.now(timezone.utc)
        job.done_event.set()
        return job

    def override_url(self, job_id: str, new_url: str) -> FetchJob:
        job = self._require(job_id)
        job.url = new_url
        return job

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {s.value: 0 for s in FetchJobStatus}
        for job in self._jobs.values():
            counts[job.status.value] += 1
        return counts

    def current_assigned(self) -> FetchJob | None:
        """Return the currently assigned (in-progress) job, if any."""
        for job in self._jobs.values():
            if job.status == FetchJobStatus.ASSIGNED:
                return job
        return None

    def _require(self, job_id: str) -> FetchJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Job not found: {job_id}")
        return job
