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


class DecisionStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"


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


@dataclass
class DecisionRequest:
    """Human decision request raised by the crawler runtime."""

    kind: str
    org_unit_name: str
    failure_count: int
    sample_urls: list[str]
    suggested_action: str = "switch_failed_to_human"
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    status: DecisionStatus = DecisionStatus.PENDING
    action: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "org_unit_name": self.org_unit_name,
            "failure_count": self.failure_count,
            "sample_urls": self.sample_urls,
            "suggested_action": self.suggested_action,
            "status": self.status.value,
            "action": self.action,
            "created_at": self.created_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }


class JobQueue:
    """In-memory job queue. Lifecycle matches the agent run."""

    def __init__(self) -> None:
        self._pending: asyncio.Queue[FetchJob] = asyncio.Queue()
        self._jobs: dict[str, FetchJob] = {}
        self._decisions: dict[str, DecisionRequest] = {}
        self._pending_decision_id: str | None = None

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

    async def request_decision(
        self,
        *,
        kind: str,
        org_unit_name: str,
        failure_count: int,
        sample_urls: list[str],
        suggested_action: str = "switch_failed_to_human",
    ) -> DecisionRequest:
        current = self.pending_decision()
        if current is not None:
            return current
        decision = DecisionRequest(
            kind=kind,
            org_unit_name=org_unit_name,
            failure_count=failure_count,
            sample_urls=sample_urls[:5],
            suggested_action=suggested_action,
        )
        self._decisions[decision.id] = decision
        self._pending_decision_id = decision.id
        return decision

    def pending_decision(self) -> DecisionRequest | None:
        if not self._pending_decision_id:
            return None
        return self._decisions.get(self._pending_decision_id)

    def resolve_decision(self, decision_id: str, action: str) -> DecisionRequest:
        decision = self._decisions.get(decision_id)
        if decision is None or decision.status != DecisionStatus.PENDING:
            raise KeyError(f"Decision not found or already resolved: {decision_id}")
        decision.status = DecisionStatus.RESOLVED
        decision.action = action
        decision.resolved_at = datetime.now(timezone.utc)
        decision.done_event.set()
        if self._pending_decision_id == decision_id:
            self._pending_decision_id = None
        return decision

    async def wait_decision(self, decision_id: str, timeout: float | None = None) -> DecisionRequest:
        decision = self._decisions.get(decision_id)
        if decision is None:
            raise KeyError(f"Decision not found: {decision_id}")
        if decision.status == DecisionStatus.RESOLVED:
            return decision
        if timeout is None:
            await decision.done_event.wait()
        else:
            await asyncio.wait_for(decision.done_event.wait(), timeout=timeout)
        return decision

    def _require(self, job_id: str) -> FetchJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"Job not found: {job_id}")
        return job
