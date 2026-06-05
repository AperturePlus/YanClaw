from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
import re
from urllib.parse import urlparse

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
from agents.crawler.org_unit_filter import (
    is_teaching_experiment_center_name,
    looks_like_sub_department_section_name,
    normalize_org_unit_match_text,
)
from agents.crawler.url_heuristics import _is_core_academic_kind
from agents.crawler.db.utils import (
    _clean_text,
    _clean_url,
    _merge_org_unit_names,
    _normalize_url,
    _now_utc,
    _row_value,
    _should_replace_org_unit_url,
)


@dataclass(frozen=True)
class SubOrgUnitSectionCandidate:
    child_id: int
    child_name: str
    child_url: str
    parent_id: int
    parent_name: str
    parent_url: str
    reason: str

    def to_evidence(self) -> dict[str, object]:
        return {
            "child_id": self.child_id,
            "child_name": self.child_name,
            "child_url": self.child_url,
            "parent_id": self.parent_id,
            "parent_name": self.parent_name,
            "parent_url": self.parent_url,
            "reason": self.reason,
        }


def _sub_org_cleanup_empty_summary() -> dict[str, int]:
    return {
        "sub_org_units_detected": 0,
        "sub_org_units_merged": 0,
        "affiliations_added": 0,
        "affiliations_deleted": 0,
        "professors_updated": 0,
        "academicians_updated": 0,
        "academicians_deleted": 0,
        "crawl_tasks_rewritten": 0,
        "crawl_tasks_deleted": 0,
        "crawl_extraction_failures_rewritten": 0,
    }


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


async def list_sub_department_section_candidates(
    session: AsyncSession,
) -> list[SubOrgUnitSectionCandidate]:
    org_units = list((await session.execute(select(OrgUnit).order_by(OrgUnit.id.asc()))).scalars().all())
    if not org_units:
        return []

    parents = [unit for unit in org_units if _looks_like_parent_org_unit(unit)]
    candidates: list[SubOrgUnitSectionCandidate] = []
    seen_children: set[int] = set()
    for child in org_units:
        if child.id is None or int(child.id) in seen_children:
            continue
        child_name = normalize_org_unit_name(child.name, default="")
        if not looks_like_sub_department_section_name(child_name):
            continue
        if is_teaching_experiment_center_name(child_name, kind=child.kind, url=child.url):
            continue

        match = _infer_sub_department_parent(child, parents)
        if match is None:
            continue
        parent, reason = match
        if parent.id is None or int(parent.id) == int(child.id):
            continue
        candidates.append(
            SubOrgUnitSectionCandidate(
                child_id=int(child.id),
                child_name=child_name,
                child_url=_normalize_url(child.url),
                parent_id=int(parent.id),
                parent_name=normalize_org_unit_name(parent.name, default=""),
                parent_url=_normalize_url(parent.url),
                reason=reason,
            )
        )
        seen_children.add(int(child.id))
    return candidates


async def merge_sub_department_sections(
    session: AsyncSession,
    candidates: list[SubOrgUnitSectionCandidate] | None = None,
) -> dict[str, int]:
    if candidates is None:
        candidates = await list_sub_department_section_candidates(session)
    summary = _sub_org_cleanup_empty_summary()
    summary["sub_org_units_detected"] = len(candidates)
    if not candidates:
        return summary

    for candidate in candidates:
        child = await session.get(OrgUnit, int(candidate.child_id))
        parent = await session.get(OrgUnit, int(candidate.parent_id))
        if child is None or parent is None:
            continue
        if child.id is None or parent.id is None or int(child.id) == int(parent.id):
            continue

        child_id = int(child.id)
        parent_id = int(parent.id)
        child_name = normalize_org_unit_name(child.name, default="")
        parent_name = normalize_org_unit_name(parent.name, default="")
        child_url = _normalize_url(child.url)
        parent_url = _normalize_url(parent.url)

        affected_professor_ids = await _merge_sub_department_affiliations(
            session,
            child_id=child_id,
            parent_id=parent_id,
            summary=summary,
        )
        await _merge_sub_department_academicians(
            session,
            child_id=child_id,
            parent_id=parent_id,
            summary=summary,
        )
        await _rewrite_sub_department_tasks_and_failures(
            session,
            child_name=child_name,
            child_url=child_url,
            parent_name=parent_name,
            parent_url=parent_url,
            summary=summary,
        )

        if affected_professor_ids:
            summary["professors_updated"] += await _refresh_professor_org_unit_names(
                session,
                sorted(affected_professor_ids),
            )

        await session.execute(delete(OrgUnit).where(OrgUnit.id == child_id))
        summary["sub_org_units_merged"] += 1

    await session.flush()
    return summary


async def cleanup_org_unit_scope_pollution(
    session: AsyncSession,
    *,
    excluded_units: list[OrgUnit] | None = None,
) -> dict[str, int]:
    summary: dict[str, int] = {}
    if excluded_units:
        summary.update(await cleanup_excluded_org_units(session, excluded_units))
    sub_summary = await merge_sub_department_sections(session)
    for key, value in sub_summary.items():
        summary[key] = int(summary.get(key, 0) or 0) + int(value or 0)
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


async def _merge_sub_department_affiliations(
    session: AsyncSession,
    *,
    child_id: int,
    parent_id: int,
    summary: dict[str, int],
) -> set[int]:
    child_affiliations = list(
        (
            await session.execute(
                select(ProfessorAffiliation).where(ProfessorAffiliation.org_unit_id == child_id)
            )
        )
        .scalars()
        .all()
    )
    affected_professor_ids = {int(row.professor_id) for row in child_affiliations if row.professor_id is not None}
    for affiliation in child_affiliations:
        existing = (
            await session.execute(
                select(ProfessorAffiliation.id)
                .where(
                    ProfessorAffiliation.professor_id == int(affiliation.professor_id),
                    ProfessorAffiliation.org_unit_id == parent_id,
                )
                .limit(1)
            )
        ).first()
        if existing is None:
            session.add(
                ProfessorAffiliation(
                    professor_id=int(affiliation.professor_id),
                    org_unit_id=parent_id,
                    source_url=affiliation.source_url,
                    created_at=_now_utc(),
                )
            )
            summary["affiliations_added"] += 1

    result = await session.execute(
        delete(ProfessorAffiliation).where(ProfessorAffiliation.org_unit_id == child_id)
    )
    summary["affiliations_deleted"] += int(result.rowcount or 0)
    await session.flush()
    return affected_professor_ids


async def _merge_sub_department_academicians(
    session: AsyncSession,
    *,
    child_id: int,
    parent_id: int,
    summary: dict[str, int],
) -> None:
    rows = list(
        (await session.execute(select(Academician).where(Academician.org_unit_id == child_id))).scalars().all()
    )
    for academician in rows:
        duplicate_conditions = [Academician.name == (academician.name or "")]
        if academician.name_key:
            duplicate_conditions.append(Academician.name_key == academician.name_key)
        duplicate = (
            await session.execute(
                select(Academician)
                .where(
                    Academician.org_unit_id == parent_id,
                    or_(*duplicate_conditions),
                )
                .order_by(Academician.id.asc())
                .limit(1)
            )
        ).scalars().first()
        if duplicate is not None:
            await session.delete(academician)
            summary["academicians_deleted"] += 1
            continue
        academician.org_unit_id = parent_id
        academician.updated_at = _now_utc()
        summary["academicians_updated"] += 1
    await session.flush()


async def _rewrite_sub_department_tasks_and_failures(
    session: AsyncSession,
    *,
    child_name: str,
    child_url: str,
    parent_name: str,
    parent_url: str,
    summary: dict[str, int],
) -> None:
    task_filters = [CrawlTask.org_unit_name == child_name]
    if child_url:
        task_filters.append(CrawlTask.org_unit_url == child_url)
    tasks = list(
        (await session.execute(select(CrawlTask).where(or_(*task_filters)).order_by(CrawlTask.id.asc())))
        .scalars()
        .all()
    )
    for task in tasks:
        duplicate = (
            await session.execute(
                select(CrawlTask)
                .where(
                    CrawlTask.id != int(task.id),
                    CrawlTask.source_url == task.source_url,
                    CrawlTask.org_unit_name == parent_name,
                    CrawlTask.page_hash == task.page_hash,
                )
                .order_by(CrawlTask.id.asc())
                .limit(1)
            )
        ).scalars().first()
        if duplicate is not None:
            result = await session.execute(
                text(
                    "UPDATE crawl_extraction_failures "
                    "SET task_id = :parent_task_id, org_unit_name = :parent_name "
                    "WHERE task_id = :child_task_id"
                ),
                {
                    "parent_task_id": int(duplicate.id),
                    "parent_name": parent_name,
                    "child_task_id": int(task.id),
                },
            )
            summary["crawl_extraction_failures_rewritten"] += int(result.rowcount or 0)
            await session.delete(task)
            summary["crawl_tasks_deleted"] += 1
            continue

        task.org_unit_name = parent_name
        task.org_unit_url = parent_url or task.org_unit_url
        task.updated_at = _now_utc()
        summary["crawl_tasks_rewritten"] += 1

    result = await session.execute(
        text(
            "UPDATE crawl_extraction_failures "
            "SET org_unit_name = :parent_name "
            "WHERE org_unit_name = :child_name"
        ),
        {"parent_name": parent_name, "child_name": child_name},
    )
    summary["crawl_extraction_failures_rewritten"] += int(result.rowcount or 0)
    await session.flush()


def _looks_like_parent_org_unit(unit: OrgUnit) -> bool:
    name = normalize_org_unit_name(getattr(unit, "name", ""), default="")
    if not name:
        return False
    if looks_like_sub_department_section_name(name):
        return False
    if is_teaching_experiment_center_name(name, kind=unit.kind, url=unit.url):
        return False
    if _is_core_academic_kind(unit.kind):
        return True
    normalized = re.sub(r"\s+", "", name)
    return bool(re.search(r"(学院|研究院|学部|书院)$", normalized))


def _infer_sub_department_parent(
    child: OrgUnit,
    parents: list[OrgUnit],
) -> tuple[OrgUnit, str] | None:
    child_id = int(child.id) if child.id is not None else None
    possible_parents = [parent for parent in parents if parent.id is not None and int(parent.id) != child_id]
    if not possible_parents:
        return None

    name_match = _unique_parent_by_name_context(child, possible_parents)
    if name_match is not None:
        return name_match, "name_contains_parent"

    url_match = _unique_parent_by_url_context(child, possible_parents)
    if url_match is not None:
        return url_match

    return None


def _unique_parent_by_name_context(child: OrgUnit, parents: list[OrgUnit]) -> OrgUnit | None:
    child_text = normalize_org_unit_match_text(
        " ".join(
            str(value or "")
            for value in (
                child.name,
                _decoded_url_text(child.url),
                _decoded_url_text(child.discovered_from_url),
            )
        )
    )
    if not child_text:
        return None
    matches: list[OrgUnit] = []
    for parent in parents:
        parent_text = normalize_org_unit_match_text(parent.name)
        if parent_text and parent_text in child_text:
            matches.append(parent)
    return matches[0] if len(matches) == 1 else None


def _unique_parent_by_url_context(child: OrgUnit, parents: list[OrgUnit]) -> tuple[OrgUnit, str] | None:
    child_url = _normalize_url(child.url)
    child_host = _url_host(child_url)
    if child_host:
        same_host = [parent for parent in parents if _url_host(parent.url) == child_host]
        if len(same_host) == 1:
            return same_host[0], "url_host_unique"

    for raw_child_url in (child.url, child.discovered_from_url):
        normalized_child_url = _normalize_url(raw_child_url)
        if not normalized_child_url:
            continue
        path_matches = [
            parent
            for parent in parents
            if _url_is_under_parent(normalized_child_url, _normalize_url(parent.url))
        ]
        if len(path_matches) == 1:
            return path_matches[0], "url_under_parent"
    return None


def _url_host(url: object) -> str:
    return (urlparse(_normalize_url(url)).hostname or "").lower()


def _url_is_under_parent(child_url: str, parent_url: str) -> bool:
    if not child_url or not parent_url:
        return False
    child = urlparse(child_url)
    parent = urlparse(parent_url)
    if not child.hostname or not parent.hostname:
        return False
    if child.hostname.lower() != parent.hostname.lower():
        return False
    parent_path = (parent.path or "/").rstrip("/")
    child_path = child.path or "/"
    if not parent_path or parent_path == "/":
        return False
    return child_path == parent_path or child_path.startswith(parent_path + "/")


def _decoded_url_text(url: object) -> str:
    value = _normalize_url(url)
    if not value:
        return ""
    parsed = urlparse(value)
    return f"{parsed.hostname or ''} {parsed.path or ''}"


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
    "SubOrgUnitSectionCandidate",
    "cleanup_org_unit_scope_pollution",
    "cleanup_excluded_org_units",
    "count_academicians",
    "count_professors",
    "count_retryable_fetch_failure_urls",
    "ensure_university_meta",
    "get_or_create_org_unit",
    "get_university_status",
    "is_url_crawled",
    "list_sub_department_section_candidates",
    "list_org_units",
    "list_retryable_fetch_failure_urls",
    "load_university_targets_from_csv",
    "log_crawl",
    "merge_sub_department_sections",
    "set_university_status",
    "set_org_unit_status",
    "_dedupe_org_units_by_name",
]
