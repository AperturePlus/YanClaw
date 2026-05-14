from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler.models import (
    Academician,
    DataQualityAudit,
    OrgUnit,
    Professor,
    ProfessorAffiliation,
    StewardRun,
)
from agents.crawler.sanitizer import normalize_org_unit_name
from agents.crawler.db.utils import _normalize_email, _normalize_homepage, _now_utc


async def create_steward_run(
    session: AsyncSession,
    *,
    mode: str,
    target_db: str,
    target_selectors: dict[str, Any] | None = None,
) -> StewardRun:
    row = StewardRun(
        mode=(mode or "dry_run").strip() or "dry_run",
        target_db=(target_db or "").strip(),
        target_selectors=json.dumps(target_selectors or {}, ensure_ascii=False),
        status="running",
        started_at=_now_utc(),
    )
    session.add(row)
    await session.flush()
    return row


async def finish_steward_run(
    session: AsyncSession,
    run_id: int,
    *,
    status: str,
    summary: dict[str, Any],
) -> StewardRun | None:
    row = await session.get(StewardRun, int(run_id))
    if row is None:
        return None
    row.status = (status or "completed").strip() or "completed"
    row.summary = json.dumps(summary, ensure_ascii=False)
    row.finished_at = _now_utc()
    await session.flush()
    return row


async def add_data_quality_audit(
    session: AsyncSession,
    *,
    run_id: int | None,
    db_name: str,
    entity_type: str,
    entity_id: int | None,
    issue_type: str,
    field_name: str | None = None,
    reason: str | None = None,
    confidence: float | None = None,
    evidence: dict[str, Any] | None = None,
    action: str = "report_only",
    before_snapshot: dict[str, Any] | None = None,
) -> DataQualityAudit:
    row = DataQualityAudit(
        run_id=run_id,
        db_name=(db_name or "").strip(),
        entity_type=(entity_type or "").strip(),
        entity_id=entity_id,
        issue_type=(issue_type or "").strip(),
        field_name=(field_name or "").strip() or None,
        reason=(reason or "").strip() or None,
        confidence=confidence,
        evidence=json.dumps(evidence or {}, ensure_ascii=False),
        action=(action or "report_only").strip() or "report_only",
        before_snapshot=json.dumps(before_snapshot, ensure_ascii=False) if before_snapshot else None,
        created_at=_now_utc(),
    )
    session.add(row)
    await session.flush()
    return row


async def match_academician_for_professor(
    session: AsyncSession,
    *,
    name: str,
    org_unit_name: str | None = None,
    email: str | None = None,
    homepage: str | None = None,
    external_link: str | None = None,
) -> tuple[Academician | None, str]:
    clean_name = str(name or "").strip()
    if not clean_name:
        return None, ""
    clean_org = normalize_org_unit_name(org_unit_name, default="")
    clean_email = _normalize_email(email)
    clean_homepage = _normalize_homepage(homepage)
    clean_external = _normalize_homepage(external_link)

    if clean_org:
        same_org = (
            await session.execute(
                select(Academician)
                .join(OrgUnit, Academician.org_unit_id == OrgUnit.id)
                .where(
                    Academician.name == clean_name,
                    OrgUnit.name == clean_org,
                )
            )
        ).scalar_one_or_none()
        if same_org is not None:
            return same_org, "same_name_org_unit"

    if clean_email:
        by_email = (
            await session.execute(
                select(Academician).where(
                    func.lower(Academician.email) == clean_email.lower(),
                )
            )
        ).scalar_one_or_none()
        if by_email is not None:
            return by_email, "email_exact"

    url_candidates = [item for item in [clean_homepage, clean_external] if item]
    for candidate in url_candidates:
        by_url = (
            await session.execute(
                select(Academician).where(
                    or_(
                        Academician.homepage == candidate,
                        Academician.external_link == candidate,
                    )
                )
            )
        ).scalar_one_or_none()
        if by_url is not None:
            return by_url, "profile_url_exact"

    return None, ""


async def merge_into_academician_from_professor(
    session: AsyncSession,
    academician: Academician,
    *,
    title: str | None = None,
    research_areas: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    homepage: str | None = None,
    external_link: str | None = None,
    bio: str | None = None,
    enrollment_pref: str | None = None,
    publications: str | None = None,
) -> bool:
    changed = False

    normalized_title = (title or "").strip() or None
    normalized_research = (research_areas or "").strip() or None
    normalized_email = _normalize_email(email)
    normalized_phone = (phone or "").strip() or None
    normalized_homepage = _normalize_homepage(homepage)
    normalized_external = _normalize_homepage(external_link)
    normalized_bio = (bio or "").strip() or None
    normalized_enrollment = (enrollment_pref or "").strip() or None
    normalized_publications = (publications or "").strip() or None

    updates: dict[str, Any] = {
        "title": normalized_title,
        "research_areas": normalized_research,
        "email": normalized_email,
        "phone": normalized_phone,
        "homepage": normalized_homepage,
        "external_link": normalized_external,
        "bio": normalized_bio,
        "enrollment_pref": normalized_enrollment,
        "publications": normalized_publications,
    }
    for field_name, value in updates.items():
        if value in {None, ""}:
            continue
        current = getattr(academician, field_name)
        if current not in {None, ""}:
            continue
        setattr(academician, field_name, value)
        changed = True

    if changed:
        academician.updated_at = _now_utc()
        await session.flush()
    return changed


async def hard_delete_professor(session: AsyncSession, professor: Professor) -> None:
    affiliations = (
        await session.execute(
            select(ProfessorAffiliation).where(ProfessorAffiliation.professor_id == professor.id)
        )
    ).scalars().all()
    for row in affiliations:
        await session.delete(row)
    await session.delete(professor)
    await session.flush()


async def delete_professor_duplicates_for_academician(
    session: AsyncSession,
    academician: Academician,
) -> int:
    if academician is None:
        return 0
    professors = (await session.execute(select(Professor).order_by(Professor.id.asc()))).scalars().all()
    deleted = 0
    for professor in professors:
        match, _reason = await match_academician_for_professor(
            session,
            name=professor.name,
            org_unit_name=professor.org_unit_name,
            email=professor.email,
            homepage=professor.homepage,
            external_link=professor.external_link,
        )
        if match is None or int(match.id) != int(academician.id):
            continue
        await hard_delete_professor(session, professor)
        deleted += 1
    return deleted


async def list_professor_academician_duplicates(
    session: AsyncSession,
) -> list[tuple[Professor, Academician, str]]:
    professors = (await session.execute(select(Professor).order_by(Professor.id.asc()))).scalars().all()
    result: list[tuple[Professor, Academician, str]] = []
    for professor in professors:
        match, reason = await match_academician_for_professor(
            session,
            name=professor.name,
            org_unit_name=professor.org_unit_name,
            email=professor.email,
            homepage=professor.homepage,
            external_link=professor.external_link,
        )
        if match is None:
            continue
        result.append((professor, match, reason))
    return result
