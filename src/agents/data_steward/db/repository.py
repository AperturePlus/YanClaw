from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler import db as crawler_db
from agents.crawler.models import (
    Academician,
    CrawlExtractionFailure,
    CrawlLog,
    CrawlTaskKind,
    CrawlTaskStatus,
    Professor,
    ProfessorAffiliation,
    UniversityMeta,
)


async def create_run(
    session: AsyncSession,
    *,
    mode: str,
    target_db: str,
) -> int:
    run = await crawler_db.create_steward_run(
        session,
        mode=mode,
        target_db=target_db,
        target_selectors={},
    )
    return int(run.id)


async def ensure_schema(session: AsyncSession, *, repair_identity: bool = True) -> None:
    await crawler_db.ensure_runtime_schema(session, repair_identity=repair_identity)


async def finish_run(
    session: AsyncSession,
    *,
    run_id: int,
    status: str,
    summary: dict[str, Any],
) -> None:
    await crawler_db.finish_steward_run(
        session,
        run_id,
        status=status,
        summary=summary,
    )


async def add_audit(
    session: AsyncSession,
    *,
    run_id: int,
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
) -> None:
    await crawler_db.add_data_quality_audit(
        session,
        run_id=run_id,
        db_name=db_name,
        entity_type=entity_type,
        entity_id=entity_id,
        issue_type=issue_type,
        field_name=field_name,
        reason=reason,
        confidence=confidence,
        evidence=evidence,
        action=action,
        before_snapshot=before_snapshot,
    )


async def list_duplicates(session: AsyncSession) -> list[tuple[Professor, Academician, str]]:
    return await crawler_db.list_professor_academician_duplicates(session)


async def list_identity_repair_candidates(session: AsyncSession) -> list[crawler_db.ProfessorIdentityRepairCandidate]:
    return await crawler_db.list_professor_identity_repair_candidates(session)


async def repair_identity_data(session: AsyncSession) -> crawler_db.ProfessorIdentityRepairSummary:
    return await crawler_db.repair_professor_identity_data(session, apply=True)


async def merge_and_optionally_delete_professor(
    session: AsyncSession,
    *,
    professor: Professor,
    academician: Academician,
    apply: bool,
) -> bool:
    if not apply:
        return False
    await crawler_db.merge_into_academician_from_professor(
        session,
        academician,
        title=professor.title,
        research_areas=professor.research_areas,
        email=professor.email,
        phone=professor.phone,
        homepage=professor.homepage,
        external_link=professor.external_link,
        bio=professor.bio,
        enrollment_pref=professor.enrollment_pref,
        publications=professor.publications,
    )
    await crawler_db.hard_delete_professor(session, professor)
    return True


async def list_professors(session: AsyncSession) -> list[Professor]:
    return (
        await session.execute(select(Professor).order_by(Professor.id.asc()))
    ).scalars().all()


async def resolve_source_url(
    session: AsyncSession,
    *,
    professor_id: int,
) -> str:
    affiliations = (
        await session.execute(
            select(ProfessorAffiliation).where(ProfessorAffiliation.professor_id == professor_id)
        )
    ).scalars().all()
    affiliation = affiliations[0] if affiliations else None
    if affiliation and (affiliation.source_url or "").strip():
        return str(affiliation.source_url).strip()
    return ""


async def count_failed_crawl_logs_by_url(
    session: AsyncSession,
    *,
    source_url: str,
) -> int:
    if not (source_url or "").strip():
        return 0
    failed_logs = (
        await session.execute(
            select(CrawlLog)
            .where(CrawlLog.status == "failed")
            .where(CrawlLog.url == source_url)
        )
    ).scalars().all()
    return len(failed_logs)


async def count_extraction_failures_by_org_unit(
    session: AsyncSession,
    *,
    org_unit_name: str,
) -> int:
    if not (org_unit_name or "").strip():
        return 0
    extraction_failures = (
        await session.execute(
            select(CrawlExtractionFailure).where(
                CrawlExtractionFailure.org_unit_name == org_unit_name
            )
        )
    ).scalars().all()
    return len(extraction_failures)


async def upsert_recrawl_task(
    session: AsyncSession,
    *,
    professor: Professor,
    source_url: str,
    last_error: str,
    priority: int = 0,
) -> bool:
    source = (source_url or "").strip()
    if not source:
        return False
    university_meta = (await session.execute(select(UniversityMeta))).scalar_one_or_none()
    university_name = str(university_meta.name) if university_meta else ""
    page_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    task = await crawler_db.upsert_crawl_task(
        session,
        university=university_name,
        org_unit_name=professor.org_unit_name or "Unknown",
        org_unit_url=None,
        source_url=source,
        page_url=source,
        page_hash=page_hash,
        task_kind=CrawlTaskKind.DETAIL_PAGE,
        page_text_snapshot="",
        allowed_tools='["save_professors"]',
        status=CrawlTaskStatus.RETRY,
        attempt=0,
        priority=priority,
        last_error=last_error,
    )
    return task is not None
