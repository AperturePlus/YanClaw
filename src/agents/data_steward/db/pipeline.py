from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Awaitable, Callable

from agents.crawler.config import CrawlerSettings
from agents.crawler.models import Professor
from . import repository
from agents.data_steward.types import StewardRunSummary
from runtime.database import DatabaseManager
from runtime.logger import get_logger


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
    missing_field_audits = 0
    recrawl_tasks_upserted = 0
    audits_written = 0
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
            missing_field_audits=missing_field_audits,
            recrawl_tasks_upserted=recrawl_tasks_upserted,
            audits_written=audits_written,
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
            missing_field_audits=missing_field_audits,
            recrawl_tasks_upserted=recrawl_tasks_upserted,
            audits_written=audits_written,
            warnings=[str(error)],
        )
    finally:
        await db.close()


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


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"
