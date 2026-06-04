from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler.models import (
    Academician,
    CrawlExtractionFailure,
    CrawlLog,
    CrawlLogStatus,
    CrawlPageCache,
    CrawlStatus,
    CrawlTask,
    OrgUnit,
    OrgUnitStatus,
    Professor,
    ProfessorAffiliation,
    UniversityMeta,
)
from agents.crawler.sanitizer import normalize_org_unit_name
from agents.crawler.db.utils import (
    _clean_text,
    _clean_url,
    _merge_org_unit_names,
    _normalize_url,
    _now_utc,
    _row_value,
    _should_replace_org_unit_url,
)


async def ensure_university_meta(
    session: AsyncSession,
    *,
    name: str,
    start_url: str,
    location: str = "",
) -> UniversityMeta:
    start_url = _clean_url(start_url)
    location = _clean_text(location)
    meta = (await session.execute(select(UniversityMeta))).scalar_one_or_none()
    if meta:
        if name and meta.name != name:
            meta.name = name
        if start_url and meta.start_url != start_url:
            meta.start_url = start_url
        if location and meta.location != location:
            meta.location = location
        meta.updated_at = _now_utc()
        await session.flush()
        return meta

    meta = UniversityMeta(
        name=name,
        start_url=start_url,
        location=location,
        crawl_status=CrawlStatus.PENDING.value,
        created_at=_now_utc(),
        updated_at=_now_utc(),
    )
    session.add(meta)
    await session.flush()
    return meta


async def get_university_status(session: AsyncSession) -> CrawlStatus | None:
    status = (await session.execute(select(UniversityMeta.crawl_status))).scalar_one_or_none()
    return CrawlStatus(status) if status else None


async def set_university_status(session: AsyncSession, status: str | CrawlStatus) -> UniversityMeta:
    status_value = status.value if isinstance(status, CrawlStatus) else str(status)
    meta = (await session.execute(select(UniversityMeta))).scalar_one_or_none()
    if not meta:
        meta = await ensure_university_meta(session, name="", start_url="")
    meta.crawl_status = status_value
    meta.updated_at = _now_utc()
    await session.flush()
    return meta


async def get_or_create_org_unit(
    session: AsyncSession,
    *,
    name: str,
    url: str,
    kind: str | None = None,
    status: str | OrgUnitStatus | None = None,
    discovered_from_url: str | None = None,
) -> OrgUnit:
    name = normalize_org_unit_name(name, default="")
    url = _normalize_url(url)
    if not name and not url:
        raise ValueError("org_unit name or url is required")
    if not url:
        url = f"about:org_unit:{name or 'Unknown'}"
    if not name:
        name = url
    kind = _clean_text(kind) if kind else None
    discovered_from_url = _normalize_url(discovered_from_url) if discovered_from_url else None
    status_value = None
    if status is not None:
        status_value = status.value if isinstance(status, OrgUnitStatus) else str(status)

    org_unit = (await session.execute(select(OrgUnit).where(OrgUnit.url == url))).scalar_one_or_none()
    if org_unit is None and name:
        org_unit = (await session.execute(select(OrgUnit).where(OrgUnit.name == name))).scalar_one_or_none()
    if org_unit:
        changed = False
        if name and org_unit.name != name:
            org_unit.name = name
            changed = True
        if url and org_unit.url != url and _should_replace_org_unit_url(org_unit.url, url):
            org_unit.url = url
            changed = True
        if kind and org_unit.kind != kind:
            org_unit.kind = kind
            changed = True
        if status_value and org_unit.status != status_value:
            org_unit.status = status_value
            changed = True
        if discovered_from_url and org_unit.discovered_from_url != discovered_from_url:
            org_unit.discovered_from_url = discovered_from_url
            changed = True
        if changed:
            org_unit.updated_at = _now_utc()
        await session.flush()
        return org_unit

    org_unit = OrgUnit(
        name=name or url,
        url=url,
        kind=kind,
        status=status_value or OrgUnitStatus.PENDING.value,
        discovered_from_url=discovered_from_url,
        created_at=_now_utc(),
        updated_at=_now_utc(),
    )
    session.add(org_unit)
    await session.flush()
    return org_unit


async def list_org_units(session: AsyncSession, *, limit: int | None = None) -> list[OrgUnit]:
    stmt = select(OrgUnit).order_by(OrgUnit.id)
    if limit is not None:
        stmt = stmt.limit(int(limit))
    return list((await session.execute(stmt)).scalars().all())


async def cleanup_excluded_org_units(
    session: AsyncSession,
    excluded_units: list[OrgUnit],
) -> dict[str, int]:
    summary = {
        "org_units_deleted": 0,
        "affiliations_deleted": 0,
        "professors_deleted": 0,
        "professors_updated": 0,
        "academicians_deleted": 0,
        "crawl_tasks_deleted": 0,
        "crawl_extraction_failures_deleted": 0,
    }
    org_unit_ids = sorted({int(unit.id) for unit in excluded_units if unit.id is not None})
    if not org_unit_ids:
        return summary

    org_unit_names = sorted(
        {
            normalize_org_unit_name(getattr(unit, "name", ""), default="")
            for unit in excluded_units
            if normalize_org_unit_name(getattr(unit, "name", ""), default="")
        }
    )
    org_unit_urls = sorted(
        {
            _normalize_url(getattr(unit, "url", ""))
            for unit in excluded_units
            if _normalize_url(getattr(unit, "url", ""))
        }
    )

    task_filters = []
    if org_unit_names:
        task_filters.append(CrawlTask.org_unit_name.in_(org_unit_names))
    if org_unit_urls:
        task_filters.append(CrawlTask.org_unit_url.in_(org_unit_urls))
    task_ids: list[int] = []
    if task_filters:
        task_ids = list(
            (await session.execute(select(CrawlTask.id).where(or_(*task_filters)))).scalars().all()
        )

    failure_filters = []
    if task_ids:
        failure_filters.append(CrawlExtractionFailure.task_id.in_(task_ids))
    if org_unit_names:
        failure_filters.append(CrawlExtractionFailure.org_unit_name.in_(org_unit_names))
    if failure_filters:
        result = await session.execute(delete(CrawlExtractionFailure).where(or_(*failure_filters)))
        summary["crawl_extraction_failures_deleted"] = int(result.rowcount or 0)

    if task_ids:
        result = await session.execute(delete(CrawlTask).where(CrawlTask.id.in_(task_ids)))
        summary["crawl_tasks_deleted"] = int(result.rowcount or 0)

    result = await session.execute(delete(Academician).where(Academician.org_unit_id.in_(org_unit_ids)))
    summary["academicians_deleted"] = int(result.rowcount or 0)

    affected_professor_ids = set(
        (
            await session.execute(
                select(ProfessorAffiliation.professor_id).where(
                    ProfessorAffiliation.org_unit_id.in_(org_unit_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    result = await session.execute(
        delete(ProfessorAffiliation).where(ProfessorAffiliation.org_unit_id.in_(org_unit_ids))
    )
    summary["affiliations_deleted"] = int(result.rowcount or 0)

    if affected_professor_ids:
        remaining_professor_ids = set(
            (
                await session.execute(
                    select(ProfessorAffiliation.professor_id).where(
                        ProfessorAffiliation.professor_id.in_(affected_professor_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        orphan_professor_ids = sorted(affected_professor_ids - remaining_professor_ids)
        if orphan_professor_ids:
            result = await session.execute(delete(Professor).where(Professor.id.in_(orphan_professor_ids)))
            summary["professors_deleted"] = int(result.rowcount or 0)
        if remaining_professor_ids:
            summary["professors_updated"] = await _refresh_professor_org_unit_names(
                session,
                sorted(remaining_professor_ids),
            )

    result = await session.execute(delete(OrgUnit).where(OrgUnit.id.in_(org_unit_ids)))
    summary["org_units_deleted"] = int(result.rowcount or 0)
    await session.flush()
    return summary


async def _refresh_professor_org_unit_names(
    session: AsyncSession,
    professor_ids: list[int],
) -> int:
    if not professor_ids:
        return 0
    rows = (
        await session.execute(
            select(ProfessorAffiliation.professor_id, OrgUnit.name)
            .join(OrgUnit, ProfessorAffiliation.org_unit_id == OrgUnit.id)
            .where(ProfessorAffiliation.professor_id.in_(professor_ids))
            .order_by(ProfessorAffiliation.professor_id.asc(), OrgUnit.id.asc())
        )
    ).all()
    names_by_professor: dict[int, list[str]] = {}
    for professor_id, org_name in rows:
        clean = normalize_org_unit_name(org_name, default="")
        if not clean:
            continue
        bucket = names_by_professor.setdefault(int(professor_id), [])
        if clean not in bucket:
            bucket.append(clean)

    updated = 0
    if not names_by_professor:
        return updated
    professors = (
        await session.execute(select(Professor).where(Professor.id.in_(names_by_professor.keys())))
    ).scalars().all()
    for professor in professors:
        merged_name = " / ".join(names_by_professor.get(int(professor.id), []))
        if merged_name and professor.org_unit_name != merged_name:
            professor.org_unit_name = merged_name
            professor.updated_at = _now_utc()
            updated += 1
    await session.flush()
    return updated


async def set_org_unit_status(
    session: AsyncSession,
    org_unit_id: int,
    status: str | OrgUnitStatus,
) -> OrgUnit | None:
    row = await session.get(OrgUnit, int(org_unit_id))
    if row is None:
        return None
    status_value = status.value if isinstance(status, OrgUnitStatus) else str(status)
    if status_value and row.status != status_value:
        row.status = status_value
        row.updated_at = _now_utc()
        await session.flush()
    return row


async def log_crawl(
    session: AsyncSession,
    url: str,
    status: str | CrawlLogStatus,
    message: str | None = None,
) -> CrawlLog:
    status_value = status.value if isinstance(status, CrawlLogStatus) else str(status)
    crawl_log = CrawlLog(
        url=_normalize_url(url),
        status=status_value,
        message=message,
        created_at=_now_utc(),
    )
    session.add(crawl_log)
    await session.flush()
    return crawl_log


async def is_url_crawled(session: AsyncSession, url: str) -> bool:
    normalized = _normalize_url(url)
    existing = (
        await session.execute(
            select(CrawlLog.id).where(
                CrawlLog.url == normalized,
                CrawlLog.status == CrawlLogStatus.SUCCESS.value,
            )
        )
    ).first()
    return existing is not None


async def list_retryable_fetch_failure_urls(
    session: AsyncSession,
    *,
    limit: int | None = None,
) -> list[str]:
    """Return unresolved fetch-layer failure URLs that should be retried on resume."""

    urls: set[str] = set()

    cache_rows = (
        await session.execute(
            select(CrawlPageCache.url, CrawlPageCache.final_url, CrawlPageCache.block_reason).where(
                CrawlPageCache.block_reason.is_not(None),
            )
        )
    ).all()
    for url, final_url, block_reason in cache_rows:
        if not _is_retryable_fetch_failure_reason(block_reason):
            continue
        for candidate in (url, final_url):
            normalized = _normalize_url(candidate)
            if normalized:
                urls.add(normalized)

    latest_log_ids = (
        select(func.max(CrawlLog.id).label("id"))
        .where(CrawlLog.url.is_not(None))
        .group_by(CrawlLog.url)
        .subquery()
    )
    log_rows = (
        await session.execute(
            select(CrawlLog.url, CrawlLog.message)
            .join(latest_log_ids, CrawlLog.id == latest_log_ids.c.id)
            .where(CrawlLog.status == CrawlLogStatus.FAILED.value)
        )
    ).all()
    for url, message in log_rows:
        if not _is_retryable_fetch_failure_reason(message):
            continue
        normalized = _normalize_url(url)
        if normalized:
            urls.add(normalized)

    ordered = sorted(urls)
    if limit is not None:
        return ordered[: max(0, int(limit))]
    return ordered


async def count_retryable_fetch_failure_urls(session: AsyncSession) -> int:
    return len(await list_retryable_fetch_failure_urls(session))


def _is_retryable_fetch_failure_reason(reason: object) -> bool:
    text_value = str(reason or "").strip().lower()
    if not text_value:
        return False
    non_retryable_markers = (
        "invalid_url",
        "human_skip",
        "no_structured_data",
        "no structured data",
        "no_faculty_page",
        "no faculty page",
        "no faculty",
    )
    if any(marker in text_value for marker in non_retryable_markers):
        return False
    retryable_markers = (
        "timeout",
        "waf",
        "challenge",
        "captcha",
        "blocked=",
        "human_failed",
    )
    return any(marker in text_value for marker in retryable_markers)


async def count_professors(session: AsyncSession) -> int:
    count = (await session.execute(select(func.count()).select_from(Professor))).scalar_one()
    return int(count or 0)


async def count_academicians(session: AsyncSession) -> int:
    count = (await session.execute(select(func.count()).select_from(Academician))).scalar_one()
    return int(count or 0)


def load_university_targets_from_csv(path: str | Path) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            name = _row_value(row, "university", "name", fallback_index=1).strip()
            url = _clean_url(_row_value(row, "url", fallback_index=2))
            location = _clean_text(_row_value(row, "location", fallback_index=3))
            if not name or not url:
                continue
            result.append({"name": name, "url": url, "location": location})
    return result


async def _dedupe_org_units_by_name(session: AsyncSession) -> None:
    rows = (await session.execute(select(OrgUnit).order_by(OrgUnit.id.asc()))).scalars().all()
    keep_by_name: dict[str, OrgUnit] = {}
    for row in rows:
        canonical_name = normalize_org_unit_name(row.name, default="Unknown")
        if row.name != canonical_name:
            row.name = canonical_name
        keeper = keep_by_name.get(canonical_name)
        if keeper is None:
            keep_by_name[canonical_name] = row
            continue

        if _should_replace_org_unit_url(keeper.url, row.url):
            keeper.url = row.url
        if (not keeper.kind) and row.kind:
            keeper.kind = row.kind
        if (not keeper.discovered_from_url) and row.discovered_from_url:
            keeper.discovered_from_url = row.discovered_from_url
        keeper.updated_at = _now_utc()

        await session.execute(
            text(
                "DELETE FROM professor_affiliations "
                "WHERE org_unit_id = :dup_id "
                "AND professor_id IN ("
                "  SELECT professor_id FROM professor_affiliations WHERE org_unit_id = :keep_id"
                ")"
            ),
            {"dup_id": row.id, "keep_id": keeper.id},
        )
        await session.execute(
            text(
                "UPDATE professor_affiliations "
                "SET org_unit_id = :keep_id "
                "WHERE org_unit_id = :dup_id"
            ),
            {"dup_id": row.id, "keep_id": keeper.id},
        )
        await session.execute(
            text(
                "DELETE FROM academicians "
                "WHERE org_unit_id = :dup_id "
                "AND name IN (SELECT name FROM academicians WHERE org_unit_id = :keep_id)"
            ),
            {"dup_id": row.id, "keep_id": keeper.id},
        )
        await session.execute(
            text(
                "UPDATE academicians "
                "SET org_unit_id = :keep_id "
                "WHERE org_unit_id = :dup_id"
            ),
            {"dup_id": row.id, "keep_id": keeper.id},
        )
        await session.execute(text("DELETE FROM org_units WHERE id = :dup_id"), {"dup_id": row.id})

    await session.flush()


__all__ = [
    "cleanup_excluded_org_units",
    "count_academicians",
    "count_professors",
    "count_retryable_fetch_failure_urls",
    "ensure_university_meta",
    "get_or_create_org_unit",
    "get_university_status",
    "is_url_crawled",
    "list_org_units",
    "list_retryable_fetch_failure_urls",
    "load_university_targets_from_csv",
    "log_crawl",
    "set_university_status",
    "set_org_unit_status",
    "_dedupe_org_units_by_name",
]
