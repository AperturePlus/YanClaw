from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Awaitable, Callable

from agents.crawler import db as crawler_db
from agents.crawler.config import CrawlerSettings
from agents.crawler.models import OrgUnit, Professor
from agents.crawler.org_unit_filter import (
    ExcludedOrgUnit,
    hard_filter_org_unit_payloads,
    llm_filter_org_unit_payloads,
    org_unit_filter_item_keys,
)
from . import repository
from agents.data_steward.types import StewardRunSummary
from runtime.database import DatabaseManager
from runtime.context import ContextManager
from runtime.llm import LLMClient
from runtime.logger import get_logger
from runtime.skills import SkillManager


UncertainClassifier = Callable[[list[dict[str, Any]], int], Awaitable[dict[int, dict[str, Any]]]]


async def process_one_database(
    *,
    settings: CrawlerSettings,
    target_db: Path,
    mode: str,
    max_context_tokens: int,
    include_backup_audit: bool,
    uncertain_classifier: UncertainClassifier | None,
) -> StewardRunSummary:
    logger = get_logger("steward.db.pipeline")
    db = DatabaseManager(_sqlite_url(target_db))
    duplicates_detected = 0
    duplicates_deleted = 0
    excluded_org_units_detected = 0
    excluded_org_units_deleted = 0
    sub_department_sections_detected = 0
    sub_department_sections_merged = 0
    missing_field_audits = 0
    recrawl_tasks_upserted = 0
    audits_written = 0
    org_unit_cleanup: dict[str, int] = {}
    run_id: int | None = None
    try:
        await db.init_db()
        async with db.session() as session:
            await repository.ensure_schema(session, repair_identity=False)
            run_id = await repository.create_run(
                session,
                mode=mode,
                target_db=str(target_db),
            )

            (
                excluded_org_units_detected,
                excluded_org_units_deleted,
                org_unit_cleanup,
                org_unit_audits,
            ) = await process_excluded_org_units(
                session,
                settings=settings,
                db=db,
                run_id=run_id,
                db_name=target_db.name,
                target_db=target_db,
                mode=mode,
                max_context_tokens=max_context_tokens,
                llm_enabled=uncertain_classifier is not None,
            )
            audits_written += org_unit_audits
            (
                sub_department_sections_detected,
                sub_department_sections_merged,
                sub_org_cleanup,
                sub_org_audits,
            ) = await process_sub_department_sections(
                session,
                settings=settings,
                run_id=run_id,
                db_name=target_db.name,
                mode=mode,
            )
            _merge_cleanup_summary(org_unit_cleanup, sub_org_cleanup)
            audits_written += sub_org_audits

            identity_candidates = await repository.list_identity_repair_candidates(session)
            for candidate in identity_candidates:
                duplicates_detected += 1
                action = "merged" if mode == "apply" else "report_only"
                await repository.add_audit(
                    session,
                    run_id=run_id,
                    db_name=target_db.name,
                    entity_type="professor",
                    entity_id=candidate.keeper_id,
                    issue_type="duplicate_professor_identity",
                    reason=candidate.reason,
                    confidence=1.0,
                    evidence={
                        "match_key": candidate.match_key,
                        "keeper_id": candidate.keeper_id,
                        "victim_ids": list(candidate.victim_ids),
                        "professor_ids": list(candidate.professor_ids),
                    },
                    action=action,
                    before_snapshot=None,
                )
                audits_written += 1
            if mode == "apply":
                if identity_candidates:
                    identity_repair = await repository.repair_identity_data(session)
                    duplicates_deleted += int(identity_repair.professors_merged or 0)
                await repository.ensure_schema(session, repair_identity=True)

            duplicates = await repository.list_duplicates(session)
            for professor, academician, reason in duplicates:
                duplicates_detected += 1
                before_snapshot = _professor_snapshot(professor)
                action = "hard_deleted" if mode == "apply" else "report_only"
                await repository.add_audit(
                    session,
                    run_id=run_id,
                    db_name=target_db.name,
                    entity_type="professor",
                    entity_id=professor.id,
                    issue_type="duplicate_academician",
                    reason=reason,
                    confidence=1.0,
                    evidence={
                        "academician_id": academician.id,
                        "academician_name": academician.name,
                    },
                    action=action,
                    before_snapshot=before_snapshot,
                )
                audits_written += 1
                deleted = await repository.merge_and_optionally_delete_professor(
                    session,
                    professor=professor,
                    academician=academician,
                    apply=(mode == "apply"),
                )
                if deleted:
                    duplicates_deleted += 1

            uncertain_pool: list[dict[str, Any]] = []
            professors = await repository.list_professors(session)
            for professor in professors:
                missing_fields: list[str] = []
                if (professor.research_areas or "").strip() == "":
                    missing_fields.append("research_areas")
                if (professor.bio or "").strip() == "":
                    missing_fields.append("bio")
                if not missing_fields:
                    continue

                static_reason, static_confidence, evidence = await infer_missing_reason(
                    session,
                    professor=professor,
                )
                if uncertain_classifier is not None and static_reason == "uncertain":
                    uncertain_pool.append(
                        {
                            "professor_id": professor.id,
                            "name": professor.name,
                            "org_unit_name": professor.org_unit_name,
                            "missing_fields": missing_fields,
                            "evidence": evidence,
                        }
                    )
                    continue

                audits_written_delta, missing_field_delta, recrawl_delta = await record_missing_field_audits(
                    session,
                    run_id=run_id,
                    db_name=target_db.name,
                    professor=professor,
                    missing_fields=missing_fields,
                    reason=static_reason,
                    confidence=static_confidence,
                    evidence=evidence,
                    apply=(mode == "apply"),
                )
                audits_written += audits_written_delta
                missing_field_audits += missing_field_delta
                recrawl_tasks_upserted += recrawl_delta

            if uncertain_classifier is not None and uncertain_pool:
                llm_result = await uncertain_classifier(uncertain_pool, max_context_tokens)
                for item in uncertain_pool:
                    professor_id = int(item["professor_id"])
                    missing_fields = [str(value) for value in item.get("missing_fields", [])]
                    evidence = dict(item.get("evidence") or {})
                    predicted = llm_result.get(professor_id, {"reason": "uncertain", "confidence": 0.35})
                    reason, confidence = validate_llm_reason(
                        reason=str(predicted.get("reason") or "uncertain"),
                        confidence=float(predicted.get("confidence") or 0.35),
                        evidence=evidence,
                    )
                    professor = next((row for row in professors if row.id == professor_id), None)
                    if professor is None:
                        continue
                    audits_written_delta, missing_field_delta, recrawl_delta = await record_missing_field_audits(
                        session,
                        run_id=run_id,
                        db_name=target_db.name,
                        professor=professor,
                        missing_fields=missing_fields,
                        reason=reason,
                        confidence=confidence,
                        evidence=evidence,
                        apply=(mode == "apply"),
                    )
                    audits_written += audits_written_delta
                    missing_field_audits += missing_field_delta
                    recrawl_tasks_upserted += recrawl_delta

            await repository.finish_run(
                session,
                run_id=run_id,
                status="completed",
                summary={
                    "duplicates_detected": duplicates_detected,
                    "duplicates_deleted": duplicates_deleted,
                    "excluded_org_units_detected": excluded_org_units_detected,
                    "excluded_org_units_deleted": excluded_org_units_deleted,
                    "sub_department_sections_detected": sub_department_sections_detected,
                    "sub_department_sections_merged": sub_department_sections_merged,
                    "org_unit_cleanup": org_unit_cleanup,
                    "missing_field_audits": missing_field_audits,
                    "recrawl_tasks_upserted": recrawl_tasks_upserted,
                    "audits_written": audits_written,
                },
            )

        backup_audit = compare_with_latest_backup(settings=settings, target_db=target_db) if include_backup_audit else {}
        return StewardRunSummary(
            db_name=target_db.name,
            mode=mode,
            status="completed",
            duplicates_detected=duplicates_detected,
            duplicates_deleted=duplicates_deleted,
            excluded_org_units_detected=excluded_org_units_detected,
            excluded_org_units_deleted=excluded_org_units_deleted,
            sub_department_sections_detected=sub_department_sections_detected,
            sub_department_sections_merged=sub_department_sections_merged,
            missing_field_audits=missing_field_audits,
            recrawl_tasks_upserted=recrawl_tasks_upserted,
            audits_written=audits_written,
            org_unit_cleanup=org_unit_cleanup,
            backup_audit=backup_audit,
        )
    except Exception as error:
        logger.exception("Data Steward pipeline failed target=%s", target_db)
        if run_id is not None:
            async with db.session() as session:
                await repository.finish_run(
                    session,
                    run_id=run_id,
                    status="failed",
                    summary={
                        "error": str(error),
                        "duplicates_detected": duplicates_detected,
                        "duplicates_deleted": duplicates_deleted,
                        "excluded_org_units_detected": excluded_org_units_detected,
                        "excluded_org_units_deleted": excluded_org_units_deleted,
                        "sub_department_sections_detected": sub_department_sections_detected,
                        "sub_department_sections_merged": sub_department_sections_merged,
                        "org_unit_cleanup": org_unit_cleanup,
                        "missing_field_audits": missing_field_audits,
                        "recrawl_tasks_upserted": recrawl_tasks_upserted,
                        "audits_written": audits_written,
                    },
                )
        return StewardRunSummary(
            db_name=target_db.name,
            mode=mode,
            status="failed",
            duplicates_detected=duplicates_detected,
            duplicates_deleted=duplicates_deleted,
            excluded_org_units_detected=excluded_org_units_detected,
            excluded_org_units_deleted=excluded_org_units_deleted,
            sub_department_sections_detected=sub_department_sections_detected,
            sub_department_sections_merged=sub_department_sections_merged,
            missing_field_audits=missing_field_audits,
            recrawl_tasks_upserted=recrawl_tasks_upserted,
            audits_written=audits_written,
            org_unit_cleanup=org_unit_cleanup,
            warnings=[str(error)],
        )
    finally:
        await db.close()


async def process_excluded_org_units(
    session: Any,
    *,
    settings: CrawlerSettings,
    db: DatabaseManager,
    run_id: int,
    db_name: str,
    target_db: Path,
    mode: str,
    max_context_tokens: int,
    llm_enabled: bool,
) -> tuple[int, int, dict[str, int], int]:
    if not settings.org_unit_exclude_enabled:
        return 0, 0, {}, 0

    org_units = await crawler_db.list_org_units(session)
    if not org_units:
        return 0, 0, {}, 0

    payloads = [_org_unit_payload(row) for row in org_units]
    hard_result = hard_filter_org_unit_payloads(
        payloads,
        exclude_enabled=True,
        keywords=list(settings.org_unit_exclude_keywords or []),
    )
    llm_result = None
    if llm_enabled and settings.openai_api_key and hard_result.kept:
        skill_manager = SkillManager(Path(settings.crawler_skills_dir), db, "crawler")
        llm_result = await llm_filter_org_unit_payloads(
            hard_result.kept,
            llm_client=LLMClient(
                settings.openai_base_url,
                settings.openai_api_key,
                settings.openai_model,
                timeout_seconds=settings.llm_timeout_seconds,
                temperature=settings.llm_temperature,
                top_p=settings.llm_top_p,
                seed=settings.llm_seed,
                max_rounds=1,
            ),
            context_manager=ContextManager(settings.openai_model),
            skills_text=skill_manager.select_for_state("EXTRACT_ORG_UNITS", set()).rendered_text,
            university=target_db.stem,
            source_url=str(target_db),
            source="data_steward",
            model_max_tokens=max_context_tokens,
            logger=get_logger("steward.db.pipeline"),
        )

    excluded = list(hard_result.hard_excluded)
    if llm_result is not None:
        excluded.extend(llm_result.llm_excluded)
    excluded_for_delete = [item for item in excluded if item.category != "sub_department_section"]
    excluded_pairs = _match_excluded_org_units(org_units, excluded_for_delete)
    if not excluded_pairs:
        return 0, 0, {}, 0

    audits_written = 0
    for org_unit, excluded_unit in excluded_pairs:
        await repository.add_audit(
            session,
            run_id=run_id,
            db_name=db_name,
            entity_type="org_unit",
            entity_id=int(org_unit.id) if org_unit.id is not None else None,
            issue_type="excluded_org_unit",
            reason=excluded_unit.category,
            confidence=1.0 if excluded_unit.source == "hard" else 0.75,
            evidence=excluded_unit.to_evidence(),
            action="hard_deleted" if mode == "apply" else "report_only",
            before_snapshot=_org_unit_snapshot(org_unit),
        )
        audits_written += 1

    cleanup_summary: dict[str, int] = {}
    deleted = 0
    if mode == "apply":
        cleanup_summary = await crawler_db.cleanup_excluded_org_units(
            session,
            [org_unit for org_unit, _excluded_unit in excluded_pairs],
        )
        deleted = int(cleanup_summary.get("org_units_deleted", 0) or 0)

    return len(excluded_pairs), deleted, cleanup_summary, audits_written


async def process_sub_department_sections(
    session: Any,
    *,
    settings: CrawlerSettings,
    run_id: int,
    db_name: str,
    mode: str,
) -> tuple[int, int, dict[str, int], int]:
    if not settings.org_unit_exclude_enabled:
        return 0, 0, {}, 0

    candidates = await crawler_db.list_sub_department_section_candidates(session)
    if not candidates:
        return 0, 0, {}, 0

    org_units_by_id = {int(unit.id): unit for unit in await crawler_db.list_org_units(session) if unit.id is not None}
    audits_written = 0
    for candidate in candidates:
        child = org_units_by_id.get(int(candidate.child_id))
        await repository.add_audit(
            session,
            run_id=run_id,
            db_name=db_name,
            entity_type="org_unit",
            entity_id=int(candidate.child_id),
            issue_type="sub_department_section",
            reason="sub_department_section",
            confidence=1.0,
            evidence=candidate.to_evidence(),
            action="merged" if mode == "apply" else "report_only",
            before_snapshot=_org_unit_snapshot(child) if child is not None else candidate.to_evidence(),
        )
        audits_written += 1

    cleanup_summary: dict[str, int] = {}
    merged = 0
    if mode == "apply":
        cleanup_summary = await crawler_db.merge_sub_department_sections(session, candidates)
        merged = int(cleanup_summary.get("sub_org_units_merged", 0) or 0)

    return len(candidates), merged, cleanup_summary, audits_written


async def infer_missing_reason(
    session: Any,
    *,
    professor: Professor,
) -> tuple[str, float, dict[str, Any]]:
    source_url = await resolve_source_url(session, professor.id)
    homepage = (professor.homepage or "").strip()

    failure_like = False
    failure_count = 0
    evidence: dict[str, Any] = {
        "source_url": source_url,
        "homepage": homepage,
        "org_unit_name": professor.org_unit_name,
    }
    crawl_failure_count = await repository.count_failed_crawl_logs_by_url(
        session,
        source_url=source_url,
    )
    failure_count += crawl_failure_count
    failure_like = failure_like or crawl_failure_count > 0

    extraction_failure_count = await repository.count_extraction_failures_by_org_unit(
        session,
        org_unit_name=professor.org_unit_name or "",
    )
    failure_count += extraction_failure_count
    failure_like = failure_like or extraction_failure_count > 0
    evidence["failure_count"] = failure_count

    if failure_like:
        return "crawl_failure", 0.85, evidence
    if source_url or homepage:
        return "site_missing", 0.75, evidence
    return "uncertain", 0.35, evidence


def validate_llm_reason(
    *,
    reason: str,
    confidence: float,
    evidence: dict[str, Any],
) -> tuple[str, float]:
    normalized_reason = reason if reason in {"crawl_failure", "site_missing", "uncertain"} else "uncertain"
    normalized_confidence = max(0.0, min(float(confidence), 1.0))
    failure_count = int(evidence.get("failure_count") or 0)
    if normalized_reason == "site_missing" and failure_count > 0:
        return "uncertain", min(normalized_confidence, 0.45)
    if normalized_reason == "crawl_failure" and failure_count <= 0:
        return "uncertain", min(normalized_confidence, 0.45)
    return normalized_reason, normalized_confidence


async def record_missing_field_audits(
    session: Any,
    *,
    run_id: int,
    db_name: str,
    professor: Professor,
    missing_fields: list[str],
    reason: str,
    confidence: float,
    evidence: dict[str, Any],
    apply: bool,
) -> tuple[int, int, int]:
    audits_written = 0
    missing_field_audits = 0
    recrawl_tasks_upserted = 0
    for field_name in missing_fields:
        await repository.add_audit(
            session,
            run_id=run_id,
            db_name=db_name,
            entity_type="professor",
            entity_id=professor.id,
            issue_type="missing_core_field",
            field_name=field_name,
            reason=reason,
            confidence=confidence,
            evidence=evidence,
            action="recrawl_enqueued" if (apply and reason == "crawl_failure") else "report_only",
        )
        audits_written += 1
        missing_field_audits += 1

    if apply and reason == "crawl_failure":
        if await enqueue_recrawl_task(session, professor):
            recrawl_tasks_upserted += 1
    return audits_written, missing_field_audits, recrawl_tasks_upserted


async def resolve_source_url(session: Any, professor_id: int) -> str:
    return await repository.resolve_source_url(session, professor_id=professor_id)


async def enqueue_recrawl_task(session: Any, professor: Professor) -> bool:
    source_url = await resolve_source_url(session, professor.id)
    if not source_url:
        source_url = (professor.homepage or "").strip()
    if not source_url:
        return False
    return await repository.upsert_recrawl_task(
        session,
        professor=professor,
        source_url=source_url,
        last_error="steward_recrawl_missing_core_fields",
    )


def compare_with_latest_backup(*, settings: CrawlerSettings, target_db: Path) -> dict[str, Any]:
    backup_root = Path(settings.university_db_dir) / "backup"
    if not backup_root.exists():
        return {}
    candidates = sorted(backup_root.rglob(target_db.name), key=lambda item: item.stat().st_mtime, reverse=True)
    if not candidates:
        return {}
    backup = candidates[0]
    active_counts = read_sqlite_counts(target_db)
    backup_counts = read_sqlite_counts(backup)
    return {
        "backup_db": str(backup),
        "active_counts": active_counts,
        "backup_counts": backup_counts,
    }


def read_sqlite_counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        professors = int(cur.execute("SELECT COUNT(*) FROM professors").fetchone()[0])
        academicians = int(cur.execute("SELECT COUNT(*) FROM academicians").fetchone()[0])
        return {
            "professors": professors,
            "academicians": academicians,
        }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _professor_snapshot(professor: Professor) -> dict[str, Any]:
    return {
        "id": professor.id,
        "name": professor.name,
        "org_unit_name": professor.org_unit_name,
        "title": professor.title,
        "research_areas": professor.research_areas,
        "email": professor.email,
        "phone": professor.phone,
        "homepage": professor.homepage,
        "external_link": professor.external_link,
        "bio": professor.bio,
        "enrollment_pref": professor.enrollment_pref,
        "publications": professor.publications,
    }


def _org_unit_payload(org_unit: OrgUnit) -> dict[str, Any]:
    return {
        "id": int(org_unit.id) if org_unit.id is not None else None,
        "name": org_unit.name,
        "url": org_unit.url,
        "kind": org_unit.kind,
    }


def _org_unit_snapshot(org_unit: OrgUnit) -> dict[str, Any]:
    return {
        "id": org_unit.id,
        "name": org_unit.name,
        "url": org_unit.url,
        "kind": org_unit.kind,
        "status": org_unit.status,
        "discovered_from_url": org_unit.discovered_from_url,
    }


def _match_excluded_org_units(
    org_units: list[OrgUnit],
    excluded: list[ExcludedOrgUnit],
) -> list[tuple[OrgUnit, ExcludedOrgUnit]]:
    excluded_by_key: dict[str, ExcludedOrgUnit] = {}
    for item in excluded:
        for key in org_unit_filter_item_keys(item.to_evidence()):
            excluded_by_key[key] = item
    if not excluded_by_key:
        return []

    result: list[tuple[OrgUnit, ExcludedOrgUnit]] = []
    seen_ids: set[int] = set()
    for org_unit in org_units:
        keys = org_unit_filter_item_keys(_org_unit_payload(org_unit))
        matched = keys & excluded_by_key.keys()
        if not matched:
            continue
        if org_unit.id is not None and int(org_unit.id) in seen_ids:
            continue
        if org_unit.id is not None:
            seen_ids.add(int(org_unit.id))
        result.append((org_unit, excluded_by_key[sorted(matched)[0]]))
    return result


def _merge_cleanup_summary(target: dict[str, int], incoming: dict[str, int]) -> None:
    for key, value in incoming.items():
        target[key] = int(target.get(key, 0) or 0) + int(value or 0)


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"
