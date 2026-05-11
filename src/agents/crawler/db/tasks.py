from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler.models import CrawlExtractionFailure, CrawlTask, CrawlTaskStatus
from agents.crawler.sanitizer import normalize_org_unit_name
from agents.crawler.db.utils import _normalize_url, _now_utc, _serialize_optional


async def upsert_crawl_task(
    session: AsyncSession,
    *,
    university: str,
    org_unit_name: str,
    org_unit_url: str | None,
    source_url: str,
    page_url: str,
    page_hash: str,
    page_text_snapshot: str,
    allowed_tools: str | None,
    attempt: int = 0,
    priority: int = 0,
    status: str | CrawlTaskStatus = CrawlTaskStatus.PENDING,
    last_error: str | None = None,
) -> CrawlTask:
    org_unit_name = normalize_org_unit_name(org_unit_name, default="Unknown")
    source_url = _normalize_url(source_url) or _normalize_url(page_url)
    page_url = _normalize_url(page_url) or source_url
    if not source_url:
        raise ValueError("source_url or page_url is required for crawl task")
    status_value = status.value if isinstance(status, CrawlTaskStatus) else str(status)

    existing = (
        await session.execute(
            select(CrawlTask).where(
                CrawlTask.source_url == source_url,
                CrawlTask.org_unit_name == org_unit_name,
                CrawlTask.page_hash == page_hash,
            )
        )
    ).scalar_one_or_none()
    if existing:
        changed = False
        if page_text_snapshot and len(existing.page_text_snapshot or "") < len(page_text_snapshot):
            existing.page_text_snapshot = page_text_snapshot
            changed = True
        if allowed_tools and existing.allowed_tools != allowed_tools:
            existing.allowed_tools = allowed_tools
            changed = True
        protected_from_pending = {
            CrawlTaskStatus.DONE.value,
            CrawlTaskStatus.FAILED.value,
            CrawlTaskStatus.IN_PROGRESS.value,
            CrawlTaskStatus.RETRY.value,
        }
        status_would_reset_active_or_terminal = (
            status_value == CrawlTaskStatus.PENDING.value
            and existing.status in protected_from_pending
        )
        if status_value and existing.status != status_value and not status_would_reset_active_or_terminal:
            existing.status = status_value
            changed = True
        if attempt > int(existing.attempt or 0):
            existing.attempt = attempt
            changed = True
        if priority != int(existing.priority or 0):
            existing.priority = priority
            changed = True
        if last_error is not None and existing.last_error != last_error:
            existing.last_error = last_error
            changed = True
        if page_url and existing.page_url != page_url:
            existing.page_url = page_url
            changed = True
        if changed:
            existing.updated_at = _now_utc()
        await session.flush()
        return existing

    row = CrawlTask(
        university=(university or "").strip(),
        org_unit_name=org_unit_name,
        org_unit_url=_normalize_url(org_unit_url) if org_unit_url else None,
        source_url=source_url,
        page_url=page_url,
        page_hash=page_hash,
        page_text_snapshot=page_text_snapshot or "",
        allowed_tools=allowed_tools,
        attempt=attempt,
        priority=priority,
        status=status_value,
        last_error=last_error,
        created_at=_now_utc(),
        updated_at=_now_utc(),
    )
    session.add(row)
    await session.flush()
    return row


async def set_crawl_task_status(
    session: AsyncSession,
    task_id: int,
    *,
    status: str | CrawlTaskStatus,
    attempt: int | None = None,
    last_error: str | None = None,
) -> CrawlTask | None:
    row = await session.get(CrawlTask, int(task_id))
    if row is None:
        return None

    status_value = status.value if isinstance(status, CrawlTaskStatus) else str(status)
    row.status = status_value
    if attempt is not None:
        row.attempt = int(attempt)
    if last_error is not None:
        row.last_error = last_error
    row.updated_at = _now_utc()
    await session.flush()
    return row


async def list_recoverable_crawl_tasks(session: AsyncSession, *, limit: int = 100) -> list[CrawlTask]:
    rows = (
        await session.execute(
            select(CrawlTask)
            .where(CrawlTask.status.in_([CrawlTaskStatus.PENDING.value, CrawlTaskStatus.RETRY.value]))
            .order_by(CrawlTask.priority.asc(), CrawlTask.id.asc())
            .limit(int(limit))
        )
    ).scalars().all()
    return list(rows)


async def log_extraction_failure(
    session: AsyncSession,
    *,
    task_id: int | None,
    failure_type: str,
    org_unit_name: str,
    source_url: str,
    raw_arguments_preview: Any | None = None,
    attempt: int = 0,
    resolver: str | None = None,
) -> CrawlExtractionFailure:
    resolver_value = _serialize_optional(resolver)
    row = CrawlExtractionFailure(
        task_id=task_id,
        failure_type=(failure_type or "").strip() or "unknown",
        org_unit_name=normalize_org_unit_name(org_unit_name, default="Unknown"),
        source_url=_normalize_url(source_url),
        raw_arguments_preview=_serialize_optional(raw_arguments_preview),
        attempt=int(attempt or 0),
        resolver=resolver_value,
        created_at=_now_utc(),
    )
    session.add(row)
    if task_id is not None and resolver_value == "retry":
        task = await session.get(CrawlTask, int(task_id))
        if task is not None:
            task.status = CrawlTaskStatus.RETRY.value
            if attempt:
                task.attempt = int(attempt)
            task.updated_at = _now_utc()
    await session.flush()
    return row


async def summarize_crawl_task_status(session: AsyncSession) -> dict[str, int]:
    result = await session.execute(
        select(CrawlTask.status, func.count()).group_by(CrawlTask.status)
    )
    summary: dict[str, int] = {
        CrawlTaskStatus.PENDING.value: 0,
        CrawlTaskStatus.IN_PROGRESS.value: 0,
        CrawlTaskStatus.RETRY.value: 0,
        CrawlTaskStatus.DONE.value: 0,
        CrawlTaskStatus.FAILED.value: 0,
    }
    for status, count in result.all():
        summary[str(status)] = int(count or 0)
    return summary


__all__ = [
    "list_recoverable_crawl_tasks",
    "log_extraction_failure",
    "set_crawl_task_status",
    "summarize_crawl_task_status",
    "upsert_crawl_task",
]
