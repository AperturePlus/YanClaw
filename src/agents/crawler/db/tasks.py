from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler.models import CrawlExtractionFailure, CrawlTask, CrawlTaskKind, CrawlTaskStatus
from agents.crawler.sanitizer import normalize_org_unit_name
from agents.crawler.db.utils import _normalize_url, _now_utc, _serialize_optional


def is_edu_cn_task_url(url: str) -> bool:
    normalized = _normalize_url(url)
    if not normalized:
        return False
    host = (urlparse(normalized).hostname or "").lower().rstrip(".")
    return host == "edu.cn" or host.endswith(".edu.cn")


def sanitize_crawl_task_url(url: str) -> str:
    normalized = _normalize_url(url)
    if not normalized or not is_edu_cn_task_url(normalized):
        return ""
    return normalized


async def cleanup_non_edu_cn_crawl_tasks(session: AsyncSession) -> dict[str, int]:
    rows = (await session.execute(select(CrawlTask))).scalars().all()
    delete_ids: list[int] = []
    org_unit_urls_cleared = 0

    for row in rows:
        source_url = sanitize_crawl_task_url(row.source_url)
        page_url = sanitize_crawl_task_url(row.page_url)
        if not source_url or not page_url:
            if row.id is not None:
                delete_ids.append(int(row.id))
            continue
        org_unit_url = sanitize_crawl_task_url(row.org_unit_url or "")
        if row.org_unit_url and not org_unit_url:
            row.org_unit_url = None
            row.updated_at = _now_utc()
            org_unit_urls_cleared += 1

    failures_deleted = 0
    deleted = 0
    if delete_ids:
        failure_result = await session.execute(
            delete(CrawlExtractionFailure).where(CrawlExtractionFailure.task_id.in_(delete_ids))
        )
        failures_deleted = int(failure_result.rowcount or 0)
        result = await session.execute(delete(CrawlTask).where(CrawlTask.id.in_(delete_ids)))
        deleted = int(result.rowcount or 0)
    await session.flush()
    return {
        "crawl_tasks_scanned": len(rows),
        "crawl_tasks_deleted": deleted,
        "crawl_extraction_failures_deleted": failures_deleted,
        "crawl_task_org_unit_urls_cleared": org_unit_urls_cleared,
    }


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
    task_kind: str | CrawlTaskKind = CrawlTaskKind.LIST_PAGE,
    attempt: int = 0,
    priority: int = 0,
    status: str | CrawlTaskStatus = CrawlTaskStatus.PENDING,
    last_error: str | None = None,
) -> CrawlTask | None:
    org_unit_name = normalize_org_unit_name(org_unit_name, default="Unknown")
    raw_source_url = _normalize_url(source_url)
    raw_page_url = _normalize_url(page_url)
    if raw_source_url:
        source_url = sanitize_crawl_task_url(raw_source_url)
        if not source_url:
            return None
    else:
        source_url = sanitize_crawl_task_url(raw_page_url)
    if raw_page_url:
        page_url = sanitize_crawl_task_url(raw_page_url)
        if not page_url:
            return None
    else:
        page_url = source_url
    if not source_url or not page_url:
        return None
    org_unit_url = sanitize_crawl_task_url(org_unit_url or "") or None
    status_value = status.value if isinstance(status, CrawlTaskStatus) else str(status)
    task_kind_value = task_kind.value if isinstance(task_kind, CrawlTaskKind) else str(task_kind)
    if task_kind_value not in {item.value for item in CrawlTaskKind}:
        task_kind_value = CrawlTaskKind.LIST_PAGE.value

    exact = (
        await session.execute(
            select(CrawlTask)
            .where(
                CrawlTask.source_url == source_url,
                CrawlTask.org_unit_name == org_unit_name,
                CrawlTask.page_hash == page_hash,
            )
            .order_by(CrawlTask.id.asc())
            .limit(1)
        )
    ).scalars().first()
    if exact:
        await _update_existing_crawl_task(
            session,
            exact,
            page_url=page_url,
            page_hash=page_hash,
            page_text_snapshot=page_text_snapshot,
            allowed_tools=allowed_tools,
            org_unit_url=org_unit_url,
            task_kind_value=task_kind_value,
            attempt=attempt,
            priority=priority,
            status_value=status_value,
            last_error=last_error,
            allow_page_hash_update=False,
            allow_task_kind_update=False,
        )
        return exact

    existing_filters = [
        CrawlTask.source_url == source_url,
        CrawlTask.org_unit_name == org_unit_name,
    ]
    if task_kind_value == CrawlTaskKind.DETAIL_PAGE.value:
        existing_filters.append(CrawlTask.task_kind == task_kind_value)
    else:
        existing_filters.append(CrawlTask.page_hash == page_hash)
    existing = (
        await session.execute(select(CrawlTask).where(*existing_filters).order_by(CrawlTask.id.asc()).limit(1))
    ).scalars().first()
    if existing:
        if (
            page_hash
            and existing.page_hash != page_hash
            and len(existing.page_text_snapshot or "") < len(page_text_snapshot or "")
        ):
            conflict = (
                await session.execute(
                    select(CrawlTask)
                    .where(
                        CrawlTask.source_url == source_url,
                        CrawlTask.org_unit_name == org_unit_name,
                        CrawlTask.page_hash == page_hash,
                        CrawlTask.id != existing.id,
                    )
                    .order_by(CrawlTask.id.asc())
                    .limit(1)
                )
            ).scalars().first()
            if conflict is not None:
                await _update_existing_crawl_task(
                    session,
                    conflict,
                    page_url=page_url,
                    page_hash=page_hash,
                    page_text_snapshot=page_text_snapshot,
                    allowed_tools=allowed_tools,
                    org_unit_url=org_unit_url,
                    task_kind_value=task_kind_value,
                    attempt=attempt,
                    priority=priority,
                    status_value=status_value,
                    last_error=last_error,
                    allow_page_hash_update=False,
                    allow_task_kind_update=False,
                )
                return conflict

        await _update_existing_crawl_task(
            session,
            existing,
            page_url=page_url,
            page_hash=page_hash,
            page_text_snapshot=page_text_snapshot,
            allowed_tools=allowed_tools,
            org_unit_url=org_unit_url,
            task_kind_value=task_kind_value,
            attempt=attempt,
            priority=priority,
            status_value=status_value,
            last_error=last_error,
            allow_page_hash_update=True,
            allow_task_kind_update=True,
        )
        return existing

    row = CrawlTask(
        university=(university or "").strip(),
        org_unit_name=org_unit_name,
        org_unit_url=org_unit_url,
        source_url=source_url,
        page_url=page_url,
        page_hash=page_hash,
        task_kind=task_kind_value,
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


async def _update_existing_crawl_task(
    session: AsyncSession,
    existing: CrawlTask,
    *,
    page_url: str,
    page_hash: str,
    page_text_snapshot: str,
    allowed_tools: str | None,
    org_unit_url: str | None,
    task_kind_value: str,
    attempt: int,
    priority: int,
    status_value: str,
    last_error: str | None,
    allow_page_hash_update: bool,
    allow_task_kind_update: bool,
) -> None:
    changed = False
    if (
        allow_page_hash_update
        and page_hash
        and existing.page_hash != page_hash
        and len(existing.page_text_snapshot or "") < len(page_text_snapshot or "")
    ):
        existing.page_hash = page_hash
        changed = True
    if page_text_snapshot and len(existing.page_text_snapshot or "") < len(page_text_snapshot):
        existing.page_text_snapshot = page_text_snapshot
        changed = True
    if allowed_tools and existing.allowed_tools != allowed_tools:
        existing.allowed_tools = allowed_tools
        changed = True
    existing_org_unit_url = sanitize_crawl_task_url(existing.org_unit_url or "") or None
    if existing.org_unit_url and not existing_org_unit_url:
        existing.org_unit_url = org_unit_url
        changed = True
    elif org_unit_url and not existing.org_unit_url:
        existing.org_unit_url = org_unit_url
        changed = True
    if allow_task_kind_update and task_kind_value and getattr(existing, "task_kind", None) != task_kind_value:
        existing.task_kind = task_kind_value
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


async def recover_stale_in_progress_crawl_tasks(session: AsyncSession) -> int:
    result = await session.execute(
        update(CrawlTask)
        .where(CrawlTask.status == CrawlTaskStatus.IN_PROGRESS.value)
        .values(
            status=CrawlTaskStatus.RETRY.value,
            last_error="recovered_stale_in_progress",
            updated_at=_now_utc(),
        )
    )
    await session.flush()
    return int(result.rowcount or 0)


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
    "cleanup_non_edu_cn_crawl_tasks",
    "is_edu_cn_task_url",
    "list_recoverable_crawl_tasks",
    "log_extraction_failure",
    "recover_stale_in_progress_crawl_tasks",
    "sanitize_crawl_task_url",
    "set_crawl_task_status",
    "summarize_crawl_task_status",
    "upsert_crawl_task",
]
