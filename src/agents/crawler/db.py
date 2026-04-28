from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlparse, urlunparse

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
from agents.crawler.sanitizer import (
    merge_enrollment_pref,
    normalize_multivalue,
    normalize_optional_text,
    normalize_org_unit_name,
    normalize_title,
)


async def ensure_university_meta(
    session: AsyncSession,
    *,
    name: str,
    start_url: str,
    location: str = "",
) -> UniversityMeta:
    """Ensure the single UniversityMeta row exists in the per-university DB."""

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
    name = _clean_text(name)
    url = _normalize_url(url)
    if not url:
        raise ValueError("org_unit url is required")
    kind = _clean_text(kind) if kind else None
    discovered_from_url = _normalize_url(discovered_from_url) if discovered_from_url else None
    status_value = None
    if status is not None:
        status_value = status.value if isinstance(status, OrgUnitStatus) else str(status)

    org_unit = (
        await session.execute(select(OrgUnit).where(OrgUnit.url == url))
    ).scalar_one_or_none()
    if org_unit:
        changed = False
        if name and org_unit.name != name:
            org_unit.name = name
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


async def upsert_professor(session: AsyncSession, data: dict[str, Any]) -> Professor:
    """Insert or update a professor with conservative cross-org_unit dedupe.

    Identity strategy:
    - Always collapse exact duplicates inside the same org unit by name.
    - Across org units, merge only when email or homepage matches after normalization.
    - Name-only matches across org units are not merged.
    - When a cross-org_unit match is found, keep one Professor row and add affiliations.
    """

    name = str(data["name"]).strip()
    if not name:
        raise ValueError("Professor name is required")

    org_unit_id = data.get("org_unit_id")
    org_unit_name = normalize_org_unit_name(
        data.get("org_unit")
        or data.get("org_unit_name")
        or data.get("college")
        or data.get("college_name"),
        default="Unknown",
    )
    if org_unit_id is None:
        org_unit_url = str(data.get("org_unit_url") or data.get("college_url") or "").strip()
        org_unit_kind = str(data.get("org_unit_kind") or "").strip() or None
        if not org_unit_url:
            # Allow missing URL when saving, but it will reduce org unit dedupe quality.
            org_unit_url = f"about:org_unit:{org_unit_name}"
        org_unit = await get_or_create_org_unit(
            session,
            name=org_unit_name,
            url=org_unit_url,
            kind=org_unit_kind,
        )
        org_unit_id = org_unit.id
    elif org_unit_name == "Unknown":
        existing_org = await session.get(OrgUnit, int(org_unit_id))
        if existing_org and existing_org.name:
            org_unit_name = normalize_org_unit_name(existing_org.name)

    email = _normalize_email(data.get("email"))
    homepage = _normalize_homepage(data.get("homepage"))
    values = {
        "name": name,
        "org_unit_name": org_unit_name,
        "title": normalize_title(data.get("title")),
        "research_areas": normalize_multivalue(data.get("research_areas")),
        "email": email,
        "phone": normalize_optional_text(data.get("phone")),
        "homepage": homepage,
        "bio": normalize_optional_text(data.get("bio")),
        "enrollment_pref": merge_enrollment_pref(
            data.get("enrollment_pref") or data.get("enrollment_preference"),
            None,
        ),
        "publications": normalize_multivalue(data.get("publications")),
    }

    professor, same_org_unit = await _find_existing_professor(
        session,
        name,
        int(org_unit_id),
        email,
        homepage,
    )
    if professor is None:
        professor = Professor(
            **values,
            created_at=_now_utc(),
            updated_at=_now_utc(),
        )
        session.add(professor)
        await session.flush()
    else:
        _merge_professor(professor, values, overwrite=same_org_unit)
        professor.updated_at = _now_utc()
        await session.flush()

    await ensure_professor_affiliation(
        session,
        professor_id=professor.id,
        org_unit_id=int(org_unit_id),
        source_url=_normalize_url(data.get("source_url")),
    )
    await session.flush()
    return professor


async def upsert_academician(session: AsyncSession, data: dict[str, Any]) -> Academician:
    name = str(data["name"]).strip()
    if not name:
        raise ValueError("Academician name is required")

    org_unit_id = data.get("org_unit_id")
    org_unit_name = normalize_org_unit_name(
        data.get("org_unit")
        or data.get("org_unit_name")
        or data.get("college")
        or data.get("college_name"),
        default="Unknown",
    )
    if org_unit_id is None:
        org_unit_url = str(data.get("org_unit_url") or data.get("college_url") or "").strip()
        org_unit_kind = str(data.get("org_unit_kind") or "").strip() or None
        if not org_unit_url:
            org_unit_url = f"about:org_unit:{org_unit_name}"
        org_unit = await get_or_create_org_unit(
            session,
            name=org_unit_name,
            url=org_unit_url,
            kind=org_unit_kind,
        )
        org_unit_id = org_unit.id

    values = {
        "name": name,
        "title": normalize_title(data.get("title")) or "院士",
        "research_areas": normalize_multivalue(data.get("research_areas")),
        "email": _normalize_email(data.get("email")),
        "phone": normalize_optional_text(data.get("phone")),
        "homepage": _normalize_homepage(data.get("homepage")),
        "bio": normalize_optional_text(data.get("bio")),
        "enrollment_pref": merge_enrollment_pref(
            data.get("enrollment_pref") or data.get("enrollment_preference"),
            None,
        ),
        "publications": normalize_multivalue(data.get("publications")),
        "source_url": _normalize_url(data.get("source_url")),
    }

    existing = (
        await session.execute(
            select(Academician).where(
                Academician.name == name,
                Academician.org_unit_id == int(org_unit_id),
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = Academician(
            **values,
            org_unit_id=int(org_unit_id),
            created_at=_now_utc(),
            updated_at=_now_utc(),
        )
        session.add(existing)
        await session.flush()
        return existing

    changed = False
    for key, value in values.items():
        if value in {None, ""}:
            continue
        current = getattr(existing, key)
        if current in {None, ""}:
            setattr(existing, key, value)
            changed = True
    if changed:
        existing.updated_at = _now_utc()
    await session.flush()
    return existing


async def ensure_professor_affiliation(
    session: AsyncSession,
    professor_id: int,
    org_unit_id: int,
    source_url: str | None = None,
) -> ProfessorAffiliation:
    existing = (
        await session.execute(
            select(ProfessorAffiliation).where(
                ProfessorAffiliation.professor_id == professor_id,
                ProfessorAffiliation.org_unit_id == org_unit_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        if source_url and existing.source_url != source_url:
            existing.source_url = source_url
        return existing

    affiliation = ProfessorAffiliation(
        professor_id=professor_id,
        org_unit_id=org_unit_id,
        source_url=source_url,
        created_at=_now_utc(),
    )
    session.add(affiliation)
    await session.flush()
    return affiliation


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
    url = _normalize_url(url)
    existing = (
        await session.execute(
            select(CrawlLog.id).where(
                CrawlLog.url == url,
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


async def ensure_runtime_schema(session: AsyncSession) -> None:
    """Best-effort lightweight schema patching for existing SQLite university DBs."""
    if session.bind is None or session.bind.dialect.name != "sqlite":
        return

    if not await _sqlite_has_column(session, "professors", "org_unit_name"):
        await session.execute(
            text("ALTER TABLE professors ADD COLUMN org_unit_name VARCHAR(255) DEFAULT 'Unknown'")
        )

    await session.execute(
        text(
            "UPDATE professors SET org_unit_name = 'Unknown' "
            "WHERE org_unit_name IS NULL OR trim(org_unit_name) = ''"
        )
    )

    await _normalize_nullish_columns(
        session,
        "professors",
        [
            "title",
            "research_areas",
            "email",
            "phone",
            "homepage",
            "bio",
            "enrollment_pref",
            "publications",
        ],
    )
    await _normalize_nullish_columns(
        session,
        "academicians",
        [
            "title",
            "research_areas",
            "email",
            "phone",
            "homepage",
            "bio",
            "enrollment_pref",
            "publications",
        ],
    )


def load_university_targets_from_csv(path: str | Path) -> list[dict[str, str]]:
    """Load university targets from a CSV-like file (assets/websites.md)."""

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


def _row_value(row: dict[str, Any], *keys: str, fallback_index: int | None = None) -> str:
    normalized = {_normalize_header(key): value for key, value in row.items()}
    for key in keys:
        value = normalized.get(_normalize_header(key))
        if value:
            return str(value)
    if fallback_index is not None:
        values = list(row.values())
        if fallback_index < len(values) and values[fallback_index] is not None:
            return str(values[fallback_index])
    return ""


def _normalize_header(value: Any) -> str:
    return str(value).strip().lower().replace(" ", "_")


async def _sqlite_has_column(session: AsyncSession, table: str, column: str) -> bool:
    result = await session.execute(text(f"PRAGMA table_info({table})"))
    rows = result.fetchall()
    for row in rows:
        # row format: cid, name, type, notnull, dflt_value, pk
        if len(row) > 1 and str(row[1]) == column:
            return True
    return False


async def _normalize_nullish_columns(
    session: AsyncSession,
    table: str,
    columns: list[str],
) -> None:
    for column in columns:
        await session.execute(
            text(
                f"UPDATE {table} SET {column} = NULL "
                f"WHERE {column} IS NOT NULL AND ("
                f"trim({column}) = '' OR lower(trim({column})) IN "
                "('null', 'none', 'n/a', 'na', 'nan', '--', '-', 'unknown')"
                ")"
            )
        )


def _serialize_optional(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _clean_url(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().strip('"').strip("'").strip()
    if text.startswith("<"):
        text = text[1:]
    if text.endswith(">"):
        text = text[:-1]
    text = text.strip()
    if text.startswith("[") and "](" in text and text.endswith(")"):
        text = text.split("](", 1)[1][:-1].strip()
    return text


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().strip("<>").strip()


async def _find_existing_professor(
    session: AsyncSession,
    name: str,
    org_unit_id: int,
    email: str | None,
    homepage: str | None,
) -> tuple[Professor | None, bool]:
    same_org_unit = (
        await session.execute(
            select(Professor)
            .join(ProfessorAffiliation, ProfessorAffiliation.professor_id == Professor.id)
            .where(
                Professor.name == name,
                ProfessorAffiliation.org_unit_id == org_unit_id,
            )
        )
    ).scalar_one_or_none()
    if same_org_unit:
        return same_org_unit, True

    if email:
        by_email = (
            await session.execute(
                select(Professor).where(func.lower(Professor.email) == email.lower())
            )
        ).scalar_one_or_none()
        if by_email:
            return by_email, False

    if homepage:
        by_homepage = (
            await session.execute(select(Professor).where(Professor.homepage == homepage))
        ).scalar_one_or_none()
        if by_homepage:
            return by_homepage, False

    return None, False


def _merge_professor(professor: Professor, values: dict[str, Any], *, overwrite: bool) -> None:
    for key, value in values.items():
        if key in {"name"} or value in {None, ""}:
            continue
        current = getattr(professor, key)
        if key == "org_unit_name":
            setattr(professor, key, _merge_org_unit_names(current, value))
            continue
        if overwrite or current in {None, ""}:
            setattr(professor, key, value)


def _normalize_email(value: Any) -> str | None:
    if value is None:
        return None
    email = str(value).strip().lower()
    return email or None


def _normalize_homepage(value: Any) -> str | None:
    if value is None:
        return None
    homepage = str(value).strip()
    if not homepage:
        return None
    homepage = urldefrag(homepage)[0]
    parsed = urlparse(homepage)
    if not parsed.scheme or not parsed.netloc:
        return homepage.rstrip("/")
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip("/"),
    )
    return urlunparse(normalized)


def _normalize_url(value: Any) -> str:
    text = _clean_url(value)
    if not text:
        return ""
    text = urldefrag(text)[0]
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.netloc:
        return text.rstrip("/")
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip("/"),
    )
    return urlunparse(normalized)


def _merge_org_unit_names(current: Any, incoming: Any) -> str:
    current_text = normalize_optional_text(current)
    incoming_text = normalize_optional_text(incoming)
    if not current_text and not incoming_text:
        return "Unknown"
    if not current_text:
        return incoming_text or "Unknown"
    if not incoming_text:
        return current_text

    existing = [part.strip() for part in current_text.split("；") if part.strip()]
    if incoming_text not in existing:
        existing.append(incoming_text)
    return "；".join(existing) if existing else "Unknown"


def _now_utc() -> Any:
    # datetime type is imported indirectly via SQLAlchemy; keep simple to avoid import cycles.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
