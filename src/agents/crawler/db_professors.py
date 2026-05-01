from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler.models import Academician, OrgUnit, Professor, ProfessorAffiliation
from agents.crawler.sanitizer import (
    merge_enrollment_pref,
    normalize_multivalue,
    normalize_optional_text,
    normalize_org_unit_name,
    normalize_title,
)
from agents.crawler.db_university import get_or_create_org_unit
from agents.crawler.db_utils import (
    _merge_org_unit_names,
    _normalize_email,
    _normalize_homepage,
    _normalize_url,
    _now_utc,
    _site_root,
    _site_root_from_url,
)


async def upsert_professor(session: AsyncSession, data: dict[str, Any]) -> Professor:
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

    source_url = _normalize_url(data.get("source_url"))
    email = _normalize_email(data.get("email"))
    org_unit_url = _normalize_url(data.get("org_unit_url") or data.get("college_url"))
    homepage, external_link = _split_profile_and_external_urls(
        source_url=source_url,
        org_unit_url=org_unit_url,
        homepage_value=data.get("homepage"),
        external_value=data.get("external_link"),
    )
    values = {
        "name": name,
        "org_unit_name": org_unit_name,
        "title": normalize_title(data.get("title")),
        "research_areas": normalize_multivalue(data.get("research_areas")),
        "email": email,
        "phone": normalize_optional_text(data.get("phone")),
        "homepage": homepage,
        "external_link": external_link,
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
        external_link,
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
        source_url=source_url,
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

    source_url = _normalize_url(data.get("source_url"))
    org_unit_url = _normalize_url(data.get("org_unit_url") or data.get("college_url"))
    homepage, external_link = _split_profile_and_external_urls(
        source_url=source_url,
        org_unit_url=org_unit_url,
        homepage_value=data.get("homepage"),
        external_value=data.get("external_link"),
    )
    values = {
        "name": name,
        "title": normalize_title(data.get("title")) or "院士",
        "research_areas": normalize_multivalue(data.get("research_areas")),
        "email": _normalize_email(data.get("email")),
        "phone": normalize_optional_text(data.get("phone")),
        "homepage": homepage,
        "external_link": external_link,
        "bio": normalize_optional_text(data.get("bio")),
        "enrollment_pref": merge_enrollment_pref(
            data.get("enrollment_pref") or data.get("enrollment_preference"),
            None,
        ),
        "publications": normalize_multivalue(data.get("publications")),
        "source_url": source_url,
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
        if key == "homepage":
            best = _choose_better_profile_url(current, value)
            if best and best != current:
                setattr(existing, key, best)
                changed = True
            continue
        if key == "external_link":
            if current in {None, ""}:
                setattr(existing, key, value)
                changed = True
            continue
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
        if source_url:
            better_source = _choose_better_profile_url(existing.source_url, source_url)
            if better_source and better_source != existing.source_url:
                existing.source_url = better_source
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


async def _find_existing_professor(
    session: AsyncSession,
    name: str,
    org_unit_id: int,
    email: str | None,
    external_link: str | None,
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

    if external_link:
        by_external_link = (
            await session.execute(select(Professor).where(Professor.external_link == external_link))
        ).scalar_one_or_none()
        if by_external_link:
            return by_external_link, False

    return None, False


def _merge_professor(professor: Professor, values: dict[str, Any], *, overwrite: bool) -> None:
    for key, value in values.items():
        if key in {"name"} or value in {None, ""}:
            continue
        current = getattr(professor, key)
        if key == "org_unit_name":
            setattr(professor, key, _merge_org_unit_names(current, value))
            continue
        if key == "homepage":
            best = _choose_better_profile_url(current, value)
            if best and best != current:
                setattr(professor, key, best)
            continue
        if key == "external_link":
            if current in {None, ""}:
                setattr(professor, key, value)
            continue
        if overwrite or current in {None, ""}:
            setattr(professor, key, value)


def _split_profile_and_external_urls(
    *,
    source_url: str,
    org_unit_url: str | None,
    homepage_value: Any,
    external_value: Any,
) -> tuple[str | None, str | None]:
    source_url = _normalize_url(source_url)
    org_unit_url = _normalize_url(org_unit_url)
    source_root = _site_root_from_url(source_url)
    org_unit_root = _site_root_from_url(org_unit_url)

    homepage: str | None = None
    external_link: str | None = None
    for candidate in (
        _normalize_homepage(homepage_value),
        _normalize_homepage(external_value),
    ):
        if not candidate:
            continue
        if _is_external_candidate(candidate, source_root=source_root, org_unit_root=org_unit_root):
            if not external_link:
                external_link = candidate
            continue
        homepage = _choose_better_profile_url(homepage, candidate)

    if source_url and _is_profile_detail_url(source_url):
        homepage = _choose_better_profile_url(homepage, source_url)

    return homepage, external_link


def _is_external_candidate(url: str, *, source_root: str, org_unit_root: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    candidate_root = _site_root(host)
    if source_root and candidate_root == source_root:
        return False
    if org_unit_root and candidate_root == org_unit_root:
        return False
    return bool(source_root or org_unit_root)


def _choose_better_profile_url(current: str | None, candidate: str | None) -> str | None:
    if not candidate:
        return current
    if not current:
        return candidate
    current_score = _profile_url_quality(current)
    candidate_score = _profile_url_quality(candidate)
    if candidate_score > current_score:
        return candidate
    if candidate_score < current_score:
        return current
    current_depth = max(0, urlparse(current).path.count("/") - 1)
    candidate_depth = max(0, urlparse(candidate).path.count("/") - 1)
    if candidate_depth > current_depth:
        return candidate
    return current


def _is_profile_detail_url(url: str) -> bool:
    return _profile_url_quality(url) >= 40


def _profile_url_quality(url: str | None) -> int:
    if not url:
        return -999
    normalized = _normalize_url(url)
    if not normalized:
        return -999
    path = urlparse(normalized).path.lower()
    if not path or path == "/":
        return -200

    score = 0
    detail_hints = ("/info/", "/profile", "/detail", "/show", "/mentor")
    list_hints = ("/szdw", "/szll", "/jsdw", "/team", "/staff", "/directory")

    if any(token in path for token in detail_hints):
        score += 70
    if re.search(r"/info/\d+/\d+(\.s?html?)?$", path):
        score += 30
    if re.search(r"/(teacher|teachers|faculty|people)/[^/]+", path):
        score += 35
    if path.endswith((".htm", ".html", ".shtml")):
        score += 4
    score += max(0, path.count("/") - 2)
    if any(token in path for token in list_hints):
        score -= 55
    return score


__all__ = [
    "ensure_professor_affiliation",
    "upsert_academician",
    "upsert_professor",
]

