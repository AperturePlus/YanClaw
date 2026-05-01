from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler.models import (
    Academician,
    CrawlLog,
    CrawlLogStatus,
    CrawlStatus,
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
    "count_academicians",
    "count_professors",
    "ensure_university_meta",
    "get_or_create_org_unit",
    "get_university_status",
    "is_url_crawled",
    "list_org_units",
    "load_university_targets_from_csv",
    "log_crawl",
    "set_university_status",
    "_dedupe_org_units_by_name",
]
