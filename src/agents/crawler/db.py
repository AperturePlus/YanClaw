from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, urlparse, urlunparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler.models import (
    College,
    CrawlLog,
    CrawlLogStatus,
    CrawlStatus,
    Professor,
    ProfessorAffiliation,
    University,
)


async def get_or_create_university(
    session: AsyncSession,
    name: str,
    url: str = "",
    location: str = "",
) -> University:
    url = _clean_url(url)
    location = _clean_text(location)
    university = (
        await session.execute(select(University).where(University.name == name))
    ).scalar_one_or_none()
    if university:
        if url and university.url != url:
            university.url = url
        if location and university.location != location:
            university.location = location
        await session.flush()
        return university

    university = University(name=name, url=url, location=location)
    session.add(university)
    await session.flush()
    return university


async def get_or_create_college(
    session: AsyncSession,
    name: str,
    university_id: int,
    url: str | None = None,
) -> College:
    url = _clean_url(url)
    college = (
        await session.execute(
            select(College).where(
                College.name == name,
                College.university_id == university_id,
            )
        )
    ).scalar_one_or_none()
    if college:
        if url and college.url != url:
            college.url = url
        await session.flush()
        return college

    college = College(name=name, university_id=university_id, url=url)
    session.add(college)
    await session.flush()
    return college


async def upsert_professor(session: AsyncSession, data: dict[str, Any]) -> Professor:
    """Insert or update a professor with conservative cross-college dedupe.

    Identity strategy:
    - Always collapse exact duplicates inside the same college by name.
    - Across colleges, merge only when email or homepage matches after normalization.
    - Name-only matches across colleges are not merged.
    - When a cross-college match is found, keep one Professor row and add affiliations.
    """

    name = str(data["name"]).strip()
    if not name:
        raise ValueError("Professor name is required")

    college_id = data.get("college_id")
    if college_id is None:
        university_name = str(data.get("university") or data.get("university_name") or "").strip()
        college_name = str(data.get("college") or data.get("college_name") or "").strip()
        if not university_name or not college_name:
            raise ValueError("university and college are required when college_id is not provided")
        university = await get_or_create_university(
            session,
            university_name,
            str(data.get("university_url") or ""),
            str(data.get("location") or ""),
        )
        college = await get_or_create_college(
            session,
            college_name,
            university.id,
            data.get("college_url"),
        )
        college_id = college.id

    email = _normalize_email(data.get("email"))
    homepage = _normalize_homepage(data.get("homepage"))
    values = {
        "name": name,
        "title": data.get("title"),
        "research_areas": _serialize_optional(data.get("research_areas")),
        "email": email,
        "phone": data.get("phone"),
        "homepage": homepage,
        "bio": data.get("bio"),
        "enrollment_pref": data.get("enrollment_pref") or data.get("enrollment_preference"),
        "publications": _serialize_optional(data.get("publications")),
        "college_id": int(college_id),
    }

    professor = await _find_existing_professor(session, name, int(college_id), email, homepage)
    if professor is None:
        professor = Professor(**values)
        session.add(professor)
        await session.flush()
    else:
        same_college = professor.college_id == int(college_id)
        _merge_professor(professor, values, overwrite=same_college)
        await session.flush()

    await ensure_professor_affiliation(session, professor.id, professor.college_id)
    await ensure_professor_affiliation(session, professor.id, int(college_id))
    await session.flush()
    return professor


async def ensure_professor_affiliation(
    session: AsyncSession,
    professor_id: int,
    college_id: int,
    source: str | None = None,
) -> ProfessorAffiliation:
    existing = (
        await session.execute(
            select(ProfessorAffiliation).where(
                ProfessorAffiliation.professor_id == professor_id,
                ProfessorAffiliation.college_id == college_id,
            )
        )
    ).scalar_one_or_none()
    if existing:
        if source and existing.source != source:
            existing.source = source
        return existing

    affiliation = ProfessorAffiliation(
        professor_id=professor_id,
        college_id=college_id,
        source=source,
    )
    session.add(affiliation)
    await session.flush()
    return affiliation


async def log_crawl(
    session: AsyncSession,
    university_id: int,
    url: str,
    status: str | CrawlLogStatus,
    message: str | None = None,
) -> CrawlLog:
    status_value = status.value if isinstance(status, CrawlLogStatus) else str(status)
    crawl_log = CrawlLog(
        university_id=university_id,
        url=url,
        status=status_value,
        message=message,
    )
    session.add(crawl_log)
    await session.flush()
    return crawl_log


async def is_url_crawled(session: AsyncSession, url: str) -> bool:
    existing = (
        await session.execute(
            select(CrawlLog.id).where(
                CrawlLog.url == url,
                CrawlLog.status == CrawlLogStatus.SUCCESS.value,
            )
        )
    ).first()
    return existing is not None


async def get_university_status(session: AsyncSession, name: str) -> CrawlStatus | None:
    status = (
        await session.execute(select(University.crawl_status).where(University.name == name))
    ).scalar_one_or_none()
    return CrawlStatus(status) if status else None


async def set_university_status(
    session: AsyncSession,
    name: str,
    status: str | CrawlStatus,
) -> University:
    status_value = status.value if isinstance(status, CrawlStatus) else str(status)
    university = (
        await session.execute(select(University).where(University.name == name))
    ).scalar_one_or_none()
    if not university:
        university = University(name=name, crawl_status=status_value)
        session.add(university)
    else:
        university.crawl_status = status_value
    await session.flush()
    return university


async def count_professors_for_university(session: AsyncSession, university_id: int) -> int:
    primary_ids = (
        select(Professor.id.label("professor_id"))
        .join(College, Professor.college_id == College.id)
        .where(College.university_id == university_id)
    )
    affiliated_ids = (
        select(ProfessorAffiliation.professor_id.label("professor_id"))
        .join(College, ProfessorAffiliation.college_id == College.id)
        .where(College.university_id == university_id)
    )
    professor_ids = primary_ids.union(affiliated_ids).subquery()
    count = (await session.execute(select(func.count()).select_from(professor_ids))).scalar_one()
    return int(count or 0)


async def load_universities_from_csv(session: AsyncSession, path: str | Path) -> list[University]:
    universities: list[University] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            name = _row_value(row, "university", "name", fallback_index=1).strip()
            url = _clean_url(_row_value(row, "url", fallback_index=2))
            location = _clean_text(_row_value(row, "location", fallback_index=3))
            if not name:
                continue
            universities.append(await get_or_create_university(session, name, url, location))
    return universities


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
    college_id: int,
    email: str | None,
    homepage: str | None,
) -> Professor | None:
    same_college = (
        await session.execute(
            select(Professor).where(
                Professor.name == name,
                Professor.college_id == college_id,
            )
        )
    ).scalar_one_or_none()
    if same_college:
        return same_college

    if email:
        by_email = (
            await session.execute(
                select(Professor).where(func.lower(Professor.email) == email.lower())
            )
        ).scalar_one_or_none()
        if by_email:
            return by_email

    if homepage:
        by_homepage = (
            await session.execute(select(Professor).where(Professor.homepage == homepage))
        ).scalar_one_or_none()
        if by_homepage:
            return by_homepage

    return None


def _merge_professor(professor: Professor, values: dict[str, Any], *, overwrite: bool) -> None:
    for key, value in values.items():
        if key in {"name", "college_id"} or value in {None, ""}:
            continue
        current = getattr(professor, key)
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
