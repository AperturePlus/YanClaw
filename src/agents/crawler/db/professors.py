from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler.models import Academician, OrgUnit, Professor, ProfessorAffiliation
from agents.crawler.sanitizer import (
    merge_enrollment_pref,
    normalize_name,
    normalize_name_key,
    normalize_multivalue,
    normalize_optional_text,
    normalize_org_unit_name,
    normalize_title,
)
from agents.crawler.url_heuristics import _is_faculty_platform, _is_query_profile_detail_url
from agents.crawler.db.university import get_or_create_org_unit
from agents.crawler.db.utils import (
    _merge_org_unit_names,
    _normalize_email,
    _normalize_homepage,
    _normalize_url,
    _now_utc,
    _site_root,
    _site_root_from_url,
)


@dataclass(frozen=True)
class UpsertEntityResult:
    entity: Professor | Academician
    status: str
    deduped_by_name_key: bool = False
    deduped_by_homepage: bool = False
    dedupe_reason: str = ""


async def upsert_professor(session: AsyncSession, data: dict[str, Any]) -> Professor:
    return (await upsert_professor_with_status(session, data)).entity  # type: ignore[return-value]


async def upsert_professor_with_status(session: AsyncSession, data: dict[str, Any]) -> UpsertEntityResult:
    name = normalize_name(data["name"])
    if not name:
        raise ValueError("Professor name is required")
    name_key = normalize_name_key(name)

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
        "name_key": name_key,
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

    professor, same_org_unit, match_reason = await _find_existing_professor(
        session,
        name_key,
        int(org_unit_id),
        email,
        homepage,
        external_link,
    )
    deduped_by_name_key = False
    deduped_by_homepage = False
    if professor is None:
        professor = Professor(
            **values,
            created_at=_now_utc(),
            updated_at=_now_utc(),
        )
        session.add(professor)
        await session.flush()
        status = "created"
    else:
        changed = _merge_professor(professor, values, overwrite=same_org_unit)
        deduped_by_name_key = match_reason == "same_org_name_key"
        deduped_by_homepage = match_reason == "homepage_exact"
        await session.flush()
        status = "updated" if changed else "unchanged"

    _affiliation, affiliation_changed = await _ensure_professor_affiliation_with_status(
        session,
        professor_id=professor.id,
        org_unit_id=int(org_unit_id),
        source_url=source_url,
    )
    if status == "unchanged" and affiliation_changed:
        status = "updated"
    await session.flush()
    return UpsertEntityResult(
        professor,
        status=status,
        deduped_by_name_key=deduped_by_name_key,
        deduped_by_homepage=deduped_by_homepage,
        dedupe_reason=match_reason,
    )


async def upsert_academician(session: AsyncSession, data: dict[str, Any]) -> Academician:
    return (await upsert_academician_with_status(session, data)).entity  # type: ignore[return-value]


async def upsert_academician_with_status(session: AsyncSession, data: dict[str, Any]) -> UpsertEntityResult:
    name = normalize_name(data["name"])
    if not name:
        raise ValueError("Academician name is required")
    name_key = normalize_name_key(name)

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
        "name_key": name_key,
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
            select(Academician)
            .where(
                Academician.name_key == name_key,
                Academician.org_unit_id == int(org_unit_id),
            )
            .order_by(Academician.id.asc())
            .limit(1)
        )
    ).scalars().first()
    match_reason = "same_org_name_key" if existing is not None else ""
    if existing is None and homepage:
        existing = (
            await session.execute(
                select(Academician)
                .where(
                    Academician.org_unit_id == int(org_unit_id),
                    Academician.homepage == homepage,
                )
                .order_by(Academician.id.asc())
                .limit(1)
            )
        ).scalars().first()
        if existing is not None:
            match_reason = "same_org_homepage"
    if existing is None and external_link:
        existing = (
            await session.execute(
                select(Academician)
                .where(
                    Academician.org_unit_id == int(org_unit_id),
                    Academician.external_link == external_link,
                )
                .order_by(Academician.id.asc())
                .limit(1)
            )
        ).scalars().first()
        if existing is not None:
            match_reason = "same_org_external_link"
    if existing is None:
        existing = Academician(
            **values,
            org_unit_id=int(org_unit_id),
            created_at=_now_utc(),
            updated_at=_now_utc(),
        )
        session.add(existing)
        await session.flush()
        return UpsertEntityResult(existing, status="created")

    changed = _merge_academician(existing, values)
    deduped_by_name_key = match_reason == "same_org_name_key"
    deduped_by_homepage = match_reason == "same_org_homepage"
    if changed:
        existing.updated_at = _now_utc()
    await session.flush()
    return UpsertEntityResult(
        existing,
        status="updated" if changed else "unchanged",
        deduped_by_name_key=deduped_by_name_key,
        deduped_by_homepage=deduped_by_homepage,
        dedupe_reason=match_reason,
    )


async def ensure_professor_affiliation(
    session: AsyncSession,
    professor_id: int,
    org_unit_id: int,
    source_url: str | None = None,
) -> ProfessorAffiliation:
    affiliation, _changed = await _ensure_professor_affiliation_with_status(
        session,
        professor_id=professor_id,
        org_unit_id=org_unit_id,
        source_url=source_url,
    )
    return affiliation


async def _ensure_professor_affiliation_with_status(
    session: AsyncSession,
    professor_id: int,
    org_unit_id: int,
    source_url: str | None = None,
) -> tuple[ProfessorAffiliation, bool]:
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
                return existing, True
        return existing, False

    affiliation = ProfessorAffiliation(
        professor_id=professor_id,
        org_unit_id=org_unit_id,
        source_url=source_url,
        created_at=_now_utc(),
    )
    session.add(affiliation)
    await session.flush()
    return affiliation, True


async def _find_existing_professor(
    session: AsyncSession,
    name_key: str,
    org_unit_id: int,
    email: str | None,
    homepage: str | None,
    external_link: str | None,
) -> tuple[Professor | None, bool, str]:
    same_org_unit = (
        await session.execute(
            select(Professor)
            .join(ProfessorAffiliation, ProfessorAffiliation.professor_id == Professor.id)
            .where(
                Professor.name_key == name_key,
                ProfessorAffiliation.org_unit_id == org_unit_id,
            )
            .order_by(Professor.id.asc())
            .limit(1)
        )
    ).scalars().first()
    if same_org_unit:
        return same_org_unit, True, "same_org_name_key"

    if email:
        by_email = (
            await session.execute(
                select(Professor).where(func.lower(Professor.email) == email.lower()).order_by(Professor.id.asc()).limit(1)
            )
        ).scalars().first()
        if by_email:
            return by_email, False, "email_exact"

    if homepage:
        by_homepage = (
            await session.execute(
                select(Professor).where(Professor.homepage == homepage).order_by(Professor.id.asc()).limit(1)
            )
        ).scalars().first()
        if by_homepage:
            return by_homepage, False, "homepage_exact"

    if external_link:
        by_external_link = (
            await session.execute(
                select(Professor).where(Professor.external_link == external_link).order_by(Professor.id.asc()).limit(1)
            )
        ).scalars().first()
        if by_external_link:
            return by_external_link, False, "external_link_exact"

    return None, False, ""


def _merge_professor(professor: Professor, values: dict[str, Any], *, overwrite: bool) -> bool:
    changed = False
    for key, value in values.items():
        if key == "name_key" or value in {None, ""}:
            continue
        if key == "name":
            best_name = _choose_better_name(professor.name, value)
            if best_name and best_name != professor.name:
                professor.name = best_name
                changed = True
            continue
        current = getattr(professor, key)
        if key == "org_unit_name":
            merged = _merge_org_unit_names(current, value)
            if merged != current:
                setattr(professor, key, merged)
                changed = True
            continue
        if key == "homepage":
            best = _choose_better_profile_url(current, value)
            if best and best != current:
                setattr(professor, key, best)
                changed = True
            continue
        if key == "title":
            best = _choose_better_title(current, value)
            if best and best != current:
                setattr(professor, key, best)
                changed = True
            continue
        if key == "external_link":
            if current in {None, ""}:
                setattr(professor, key, value)
                changed = True
            continue
        if (overwrite or current in {None, ""}) and current != value:
            setattr(professor, key, value)
            changed = True
    normalized_key = normalize_name_key(professor.name)
    if professor.name_key != normalized_key:
        professor.name_key = normalized_key
        changed = True
    if changed:
        professor.updated_at = _now_utc()
    return changed


def _merge_academician(academician: Academician, values: dict[str, Any]) -> bool:
    changed = False
    for key, value in values.items():
        if key == "name_key" or value in {None, ""}:
            continue
        current = getattr(academician, key)
        if key == "name":
            best_name = _choose_better_name(current, value)
            if best_name and best_name != current:
                setattr(academician, key, best_name)
                changed = True
            continue
        if key == "homepage":
            best = _choose_better_profile_url(current, value)
            if best and best != current:
                setattr(academician, key, best)
                changed = True
            continue
        if key == "external_link":
            if current in {None, ""}:
                setattr(academician, key, value)
                changed = True
            continue
        if current in {None, ""}:
            setattr(academician, key, value)
            changed = True
    normalized_key = normalize_name_key(academician.name)
    if academician.name_key != normalized_key:
        academician.name_key = normalized_key
        changed = True
    return changed


def _choose_better_name(current: Any, incoming: Any) -> str:
    current_name = normalize_name(current)
    incoming_name = normalize_name(incoming)
    if not current_name:
        return incoming_name
    if not incoming_name:
        return current_name
    if normalize_name_key(current_name) == normalize_name_key(incoming_name) and current_name != incoming_name:
        return incoming_name
    return current_name


_TITLE_QUALITY: dict[str, int] = {
    "院士": 100,
    "教授": 80,
    "研究员": 78,
    "主任医师": 76,
    "副教授": 70,
    "副研究员": 68,
    "副主任医师": 66,
    "助理教授": 55,
    "助理研究员": 53,
    "主治医师": 50,
    "讲师": 40,
    "工程师": 35,
    "住院医师": 30,
    "博士后": 25,
}


def _choose_better_title(current: Any, incoming: Any) -> str | None:
    current_title = normalize_title(current)
    incoming_title = normalize_title(incoming)
    if not current_title:
        return incoming_title
    if not incoming_title:
        return current_title
    current_quality = _TITLE_QUALITY.get(current_title, 0)
    incoming_quality = _TITLE_QUALITY.get(incoming_title, 0)
    if incoming_quality > current_quality:
        return incoming_title
    return current_title


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
        if _is_faculty_platform(candidate):
            if not external_link:
                external_link = candidate
            continue
        homepage = _choose_better_profile_url(homepage, normalize_professor_homepage(candidate))

    source_homepage = normalize_professor_homepage(source_url)
    if source_homepage:
        if _is_faculty_platform(source_homepage):
            if not external_link:
                external_link = source_homepage
        else:
            homepage = _choose_better_profile_url(homepage, source_homepage)

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


def normalize_professor_homepage(value: Any) -> str | None:
    normalized = _normalize_homepage(value)
    if not normalized:
        return None
    if not _is_usable_profile_homepage(normalized):
        return None
    return normalized


def _is_usable_profile_homepage(url: str) -> bool:
    normalized = _normalize_url(url)
    if not normalized or normalized.startswith("about:"):
        return False
    parsed = urlparse(normalized)
    if not parsed.scheme or not parsed.netloc:
        return False
    path = (parsed.path or "").lower().rstrip("/")
    if not path or path == "/":
        return False
    tail = path.rsplit("/", 1)[-1]
    stem = tail.rsplit(".", 1)[0]
    if _is_query_profile_detail_url(normalized):
        return True
    directory_stems = {
        "faculty",
        "teacher",
        "teachers",
        "staff",
        "directory",
        "team",
        "szdw",
        "jsdw",
        "szll",
        "jzg",
        "jsml",
        "list",
        "teamlist",
        "teacherlist",
        "facultylist",
        "tu-list",
    }
    if stem in directory_stems:
        return False
    return _profile_url_quality(normalized) >= 0


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

    if _is_query_profile_detail_url(normalized):
        score += 95
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
    "UpsertEntityResult",
    "ensure_professor_affiliation",
    "normalize_professor_homepage",
    "upsert_academician",
    "upsert_academician_with_status",
    "upsert_professor",
    "upsert_professor_with_status",
]
