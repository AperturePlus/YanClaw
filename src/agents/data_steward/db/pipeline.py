from __future__ import annotations

from dataclasses import dataclass
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import or_, select

from agents.crawler import db as crawler_db
from agents.crawler.config import CrawlerSettings
from agents.crawler.db.utils import _normalize_url, _now_utc
from agents.crawler.models import Academician, CrawlTask, CrawlTaskKind, CrawlTaskStatus, OrgUnit, Professor
from agents.crawler.org_unit_filter import (
    ExcludedOrgUnit,
    hard_filter_org_unit_payloads,
    llm_filter_org_unit_payloads,
    org_unit_filter_item_keys,
)
from agents.crawler.sanitizer import (
    contains_academician_hint,
    contains_self_academician_hint,
    infer_research_areas_from_bio,
    normalize_non_academician_title,
)
from . import repository
from agents.data_steward.llm_service import STEWARD_ORG_UNIT_CLEANUP
from agents.data_steward.types import StewardRunSummary
from runtime.database import DatabaseManager
from runtime.context import ContextManager
from runtime.llm import LLMClient
from runtime.logger import get_logger
from runtime.skills import SkillManager


StewardLLMFactory = Callable[[DatabaseManager], Any]

HOMEPAGE_PROFILE_INCOMPLETE_REASON = "homepage_profile_incomplete"
COMPLETION_RECRAWL_LAST_ERROR = "completion_recrawl_missing_research_areas"
COMPLETION_RECRAWL_PROFILE_FIELDS_LAST_ERROR = "completion_recrawl_missing_profile_fields"
COMPLETION_RECRAWL_REPAIRED_LAST_ERROR = "completion_recrawl_repaired_from_structured_evidence"
BIO_ACADEMICIAN_REASON = "bio_academician_hint"
BIO_INFERENCE_REASON = "bio_inference"
PROFILE_SNAPSHOT_ACADEMICIAN_REASON = "profile_snapshot_academician_hint"
PROFILE_SNAPSHOT_INFERENCE_REASON = "profile_snapshot_inference"
MISCLASSIFIED_ACADEMICIAN_REASON = "no_self_academician_evidence"
_TITLE_RELATION_SENTENCE_RE = re.compile(
    r"(?:^|[，,；;\s])(?:与|同|和|跟)[^。；;\n\r]{0,120}?"
    r"(?:教授|副教授|研究员|院士|professor|researcher)[^。；;\n\r]{0,80}?合作|"
    r"(?:师从|合作导师|合作学者|合作对象)[^。；;\n\r]{0,120}?"
    r"(?:教授|副教授|研究员|院士|professor|researcher)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProfileSnapshotEvidence:
    task_id: int
    source_url: str
    page_url: str
    text: str
    name_window: str


async def process_one_database(
    *,
    settings: CrawlerSettings,
    target_db: Path,
    mode: str,
    max_context_tokens: int,
    include_backup_audit: bool,
    steward_llm_factory: StewardLLMFactory | None,
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
        logger.info(
            "Data Steward pipeline start db=%s mode=%s llm_enabled=%s include_backup_audit=%s",
            target_db,
            mode,
            steward_llm_factory is not None,
            include_backup_audit,
        )
        await db.init_db()
        logger.info("Data Steward DB initialized db=%s", target_db)
        steward_llm = steward_llm_factory(db) if steward_llm_factory is not None else None
        async with db.session() as session:
            await repository.ensure_schema(session, repair_identity=False)
            run_id = await repository.create_run(
                session,
                mode=mode,
                target_db=str(target_db),
            )
            logger.info(
                "Data Steward run created db=%s run_id=%s mode=%s",
                target_db.name,
                run_id,
                mode,
            )

            logger.info("Data Steward excluded org-unit cleanup start db=%s", target_db.name)
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
                llm_enabled=steward_llm is not None,
            )
            audits_written += org_unit_audits
            logger.info(
                "Data Steward excluded org-unit cleanup done db=%s detected=%s deleted=%s audits=%s",
                target_db.name,
                excluded_org_units_detected,
                excluded_org_units_deleted,
                org_unit_audits,
            )
            logger.info("Data Steward sub-department cleanup start db=%s", target_db.name)
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
            logger.info(
                "Data Steward sub-department cleanup done db=%s detected=%s merged=%s audits=%s",
                target_db.name,
                sub_department_sections_detected,
                sub_department_sections_merged,
                sub_org_audits,
            )

            logger.info("Data Steward identity repair scan start db=%s", target_db.name)
            identity_candidates = await repository.list_identity_repair_candidates(session)
            logger.info(
                "Data Steward identity repair candidates db=%s candidates=%s",
                target_db.name,
                len(identity_candidates),
            )
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
                    logger.info(
                        "Data Steward identity repair applied db=%s merged=%s",
                        target_db.name,
                        int(identity_repair.professors_merged or 0),
                    )
                await repository.ensure_schema(session, repair_identity=True)

            logger.info("Data Steward academician duplicate scan start db=%s", target_db.name)
            duplicates = await repository.list_duplicates(session)
            logger.info(
                "Data Steward academician duplicate candidates db=%s candidates=%s",
                target_db.name,
                len(duplicates),
            )
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

            logger.info("Data Steward structured profile inference start db=%s", target_db.name)
            structured_audits = await process_bio_structured_inferences(
                session,
                run_id=run_id,
                db_name=target_db.name,
                apply=(mode == "apply"),
                steward_llm=steward_llm,
                max_context_tokens=max_context_tokens,
            )
            audits_written += structured_audits
            logger.info(
                "Data Steward structured profile inference done db=%s audits=%s",
                target_db.name,
                structured_audits,
            )

            missing_pool: list[dict[str, Any]] = []
            professors = await repository.list_professors(session)
            logger.info(
                "Data Steward missing-field audit start db=%s professors=%s llm_enabled=%s",
                target_db.name,
                len(professors),
                steward_llm is not None,
            )
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
                if steward_llm is not None:
                    missing_pool.append(
                        {
                            "id": professor.id,
                            "professor_id": professor.id,
                            "name": professor.name,
                            "org_unit_name": professor.org_unit_name,
                            "missing_fields": missing_fields,
                            "static_reason": static_reason,
                            "static_confidence": static_confidence,
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

            if steward_llm is not None and missing_pool:
                logger.info(
                    "Data Steward missing-field LLM triage start db=%s rows=%s",
                    target_db.name,
                    len(missing_pool),
                )
                llm_result = await steward_llm.classify_missing_fields(missing_pool, max_context_tokens)
                logger.info(
                    "Data Steward missing-field LLM triage done db=%s rows=%s decisions=%s",
                    target_db.name,
                    len(missing_pool),
                    len(llm_result),
                )
                for item in missing_pool:
                    professor_id = int(item["professor_id"])
                    missing_fields = [str(value) for value in item.get("missing_fields", [])]
                    evidence = dict(item.get("evidence") or {})
                    predicted = llm_result.get(
                        professor_id,
                        {
                            "reason": item.get("static_reason") or "uncertain",
                            "confidence": item.get("static_confidence") or 0.35,
                        },
                    )
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
            logger.info(
                "Data Steward missing-field audit done db=%s missing_audits=%s recrawl_tasks=%s audits_written=%s",
                target_db.name,
                missing_field_audits,
                recrawl_tasks_upserted,
                audits_written,
            )

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
            logger.info("Data Steward run marked completed db=%s run_id=%s", target_db.name, run_id)

        if include_backup_audit:
            logger.info("Data Steward backup audit start db=%s", target_db.name)
            backup_audit = compare_with_latest_backup(settings=settings, target_db=target_db)
            logger.info(
                "Data Steward backup audit done db=%s keys=%s",
                target_db.name,
                len(backup_audit),
            )
        else:
            backup_audit = {}
        summary = StewardRunSummary(
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
        logger.info(
            "Data Steward pipeline done db=%s status=%s duplicates=%s deleted=%s "
            "excluded_org_units=%s excluded_org_units_deleted=%s sub_department_sections=%s "
            "sub_department_sections_merged=%s missing_audits=%s recrawl_tasks=%s audits_written=%s",
            summary.db_name,
            summary.status,
            summary.duplicates_detected,
            summary.duplicates_deleted,
            summary.excluded_org_units_detected,
            summary.excluded_org_units_deleted,
            summary.sub_department_sections_detected,
            summary.sub_department_sections_merged,
            summary.missing_field_audits,
            summary.recrawl_tasks_upserted,
            summary.audits_written,
        )
        return summary
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
        logger.warning(
            "Data Steward pipeline failed summary db=%s run_id=%s duplicates=%s deleted=%s "
            "excluded_org_units=%s excluded_org_units_deleted=%s sub_department_sections=%s "
            "sub_department_sections_merged=%s missing_audits=%s recrawl_tasks=%s audits_written=%s error=%s",
            target_db.name,
            run_id,
            duplicates_detected,
            duplicates_deleted,
            excluded_org_units_detected,
            excluded_org_units_deleted,
            sub_department_sections_detected,
            sub_department_sections_merged,
            missing_field_audits,
            recrawl_tasks_upserted,
            audits_written,
            error,
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
        skill_manager = SkillManager(Path(settings.data_steward_skills_dir), db, "data_steward")
        llm_result = await llm_filter_org_unit_payloads(
            hard_result.kept,
            llm_client=LLMClient(
                settings.openai_base_url,
                settings.openai_api_key,
                settings.openai_model,
                max_concurrent=settings.llm_max_concurrent,
                min_interval=settings.llm_min_interval_seconds,
                timeout_seconds=settings.llm_timeout_seconds,
                temperature=settings.llm_temperature,
                top_p=settings.llm_top_p,
                seed=settings.llm_seed,
                max_rounds=1,
            ),
            context_manager=ContextManager(settings.openai_model),
            skills_text=skill_manager.select_for_state(STEWARD_ORG_UNIT_CLEANUP, set()).rendered_text,
            university=target_db.stem,
            source_url=str(target_db),
            source="data_steward",
            state=STEWARD_ORG_UNIT_CLEANUP,
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


async def process_bio_structured_inferences(
    session: Any,
    *,
    run_id: int,
    db_name: str,
    apply: bool,
    steward_llm: Any | None = None,
    max_context_tokens: int = 128000,
) -> int:
    audits_written = 0
    snapshot_evidence = await _load_profile_snapshot_evidence(session)
    identity_reviewed: set[str] = set()
    if steward_llm is not None:
        identity_delta, identity_reviewed = await _apply_llm_identity_reviews(
            session,
            run_id=run_id,
            db_name=db_name,
            apply=apply,
            snapshot_evidence=snapshot_evidence,
            steward_llm=steward_llm,
            max_context_tokens=max_context_tokens,
        )
        audits_written += identity_delta
        audits_written += await _apply_llm_profile_cleanup(
            session,
            run_id=run_id,
            db_name=db_name,
            apply=apply,
            snapshot_evidence=snapshot_evidence,
            steward_llm=steward_llm,
            max_context_tokens=max_context_tokens,
        )
    audits_written += await _demote_misclassified_academicians(
        session,
        run_id=run_id,
        db_name=db_name,
        apply=apply,
        snapshot_evidence=snapshot_evidence,
        identity_reviewed=identity_reviewed,
    )
    professors = await repository.list_professors(session)
    for professor in professors:
        before_snapshot = _professor_snapshot(professor)
        if _entity_key("professor", professor.id) in identity_reviewed:
            continue
        evidence = _snapshot_evidence_for_entity(
            snapshot_evidence,
            name=professor.name,
            urls=[professor.homepage, professor.external_link],
        )
        inferred_research, research_reason, research_evidence = _infer_research_from_profile_evidence(
            bio=professor.bio,
            snapshot_evidence=evidence,
        )
        academician_reason = _academician_reason_from_profile_evidence(
            name=professor.name,
            title=professor.title,
            bio=professor.bio,
            snapshot_evidence=evidence,
        )
        if academician_reason:
            promotion_evidence = research_evidence
            if promotion_evidence is None and academician_reason == PROFILE_SNAPSHOT_ACADEMICIAN_REASON:
                promotion_evidence = evidence
            await repository.add_audit(
                session,
                run_id=run_id,
                db_name=db_name,
                entity_type="professor",
                entity_id=professor.id,
                issue_type="promoted_academician",
                reason=academician_reason,
                confidence=0.95,
                evidence={
                    "name": professor.name,
                    "org_unit_name": professor.org_unit_name,
                    "inferred_research_areas": inferred_research,
                    **_profile_evidence_payload(promotion_evidence),
                },
                action="promoted" if apply else "report_only",
                before_snapshot=before_snapshot,
            )
            audits_written += 1
            if apply:
                await _mark_completion_recrawl_tasks_resolved(
                    session,
                    urls=[professor.homepage, professor.external_link],
                    evidence=evidence,
                )
                await crawler_db.upsert_academician_with_status(
                    session,
                    {
                        "name": professor.name,
                        "org_unit_name": professor.org_unit_name,
                        "title": "院士",
                        "research_areas": professor.research_areas or inferred_research,
                        "email": professor.email,
                        "phone": professor.phone,
                        "homepage": professor.homepage,
                        "external_link": professor.external_link,
                        "bio": professor.bio,
                        "enrollment_pref": professor.enrollment_pref,
                        "publications": professor.publications,
                        "source_url": professor.homepage,
                    },
                )
                await crawler_db.hard_delete_professor(session, professor)
            continue

        if inferred_research and not (professor.research_areas or "").strip():
            await repository.add_audit(
                session,
                run_id=run_id,
                db_name=db_name,
                entity_type="professor",
                entity_id=professor.id,
                issue_type="inferred_research_areas",
                field_name="research_areas",
                reason=research_reason,
                confidence=0.80,
                evidence={
                    "inferred_research_areas": inferred_research,
                    **_profile_evidence_payload(research_evidence),
                },
                action="updated" if apply else "report_only",
                before_snapshot=before_snapshot,
            )
            audits_written += 1
            if apply:
                professor.research_areas = inferred_research
                professor.updated_at = _now_utc()
                await _mark_completion_recrawl_tasks_resolved(
                    session,
                    urls=[professor.homepage, professor.external_link],
                    evidence=research_evidence,
                )

    academicians = (await session.execute(select(Academician).order_by(Academician.id.asc()))).scalars().all()
    for academician in academicians:
        if (academician.research_areas or "").strip():
            continue
        evidence = _snapshot_evidence_for_entity(
            snapshot_evidence,
            name=academician.name,
            urls=[academician.homepage, academician.external_link, academician.source_url],
        )
        inferred_research, research_reason, research_evidence = _infer_research_from_profile_evidence(
            bio=academician.bio,
            snapshot_evidence=evidence,
        )
        if not inferred_research:
            continue
        await repository.add_audit(
            session,
            run_id=run_id,
            db_name=db_name,
            entity_type="academician",
            entity_id=academician.id,
            issue_type="inferred_research_areas",
            field_name="research_areas",
            reason=research_reason,
            confidence=0.80,
            evidence={
                "inferred_research_areas": inferred_research,
                **_profile_evidence_payload(research_evidence),
            },
            action="updated" if apply else "report_only",
            before_snapshot=_academician_snapshot(academician),
        )
        audits_written += 1
        if apply:
            academician.research_areas = inferred_research
            academician.updated_at = _now_utc()
            await _mark_completion_recrawl_tasks_resolved(
                session,
                urls=[academician.homepage, academician.external_link, academician.source_url],
                evidence=research_evidence,
            )
    await session.flush()
    return audits_written


async def _apply_llm_profile_cleanup(
    session: Any,
    *,
    run_id: int,
    db_name: str,
    apply: bool,
    snapshot_evidence: dict[str, list[ProfileSnapshotEvidence]],
    steward_llm: Any,
    max_context_tokens: int,
) -> int:
    rows, text_by_key, entity_by_key, evidence_by_key = await _profile_cleanup_rows(session, snapshot_evidence)
    if not rows:
        return 0
    decisions = await steward_llm.cleanup_profiles(rows, max_context_tokens)
    audits_written = 0
    for row in rows:
        key = str(row["entity_key"])
        decision = decisions.get(key)
        if not isinstance(decision, dict):
            continue
        entity = entity_by_key.get(key)
        if entity is None:
            continue
        updates = decision.get("updates")
        if not isinstance(updates, dict):
            continue
        confidence = _safe_confidence(decision.get("confidence"), default=0.0)
        profile_text = text_by_key.get(key, "")
        before_snapshot = _entity_snapshot(row["entity_type"], entity)
        for field_name in ("bio", "research_areas", "title", "enrollment_pref"):
            value = _clean_llm_text(updates.get(field_name))
            if not value or not _is_empty_text(getattr(entity, field_name, None)):
                continue
            spans = _evidence_spans_for_field(decision, field_name)
            evidence_ok = confidence >= 0.75 and _evidence_spans_exist(spans, profile_text)
            await repository.add_audit(
                session,
                run_id=run_id,
                db_name=db_name,
                entity_type=str(row["entity_type"]),
                entity_id=int(getattr(entity, "id")),
                issue_type="llm_profile_cleanup",
                field_name=field_name,
                reason=str(decision.get("reason") or "llm_profile_cleanup"),
                confidence=confidence,
                evidence={
                    "field_update": value,
                    "evidence_spans": spans,
                    "evidence_valid": evidence_ok,
                    "recrawl_needed": bool(decision.get("recrawl_needed")),
                    "skill_state": "STEWARD_PROFILE_CLEANUP",
                    **_profile_evidence_payload(evidence_by_key.get(key)),
                },
                action="updated" if (apply and evidence_ok) else "report_only",
                before_snapshot=before_snapshot,
            )
            audits_written += 1
            if apply and evidence_ok:
                setattr(entity, field_name, value)
                entity.updated_at = _now_utc()
                await _mark_completion_recrawl_tasks_resolved(
                    session,
                    urls=[getattr(entity, "homepage", None), getattr(entity, "external_link", None)],
                    evidence=evidence_by_key.get(key),
                )
    if audits_written and apply:
        await session.flush()
    return audits_written


async def _profile_cleanup_rows(
    session: Any,
    snapshot_evidence: dict[str, list[ProfileSnapshotEvidence]],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any], dict[str, ProfileSnapshotEvidence | None]]:
    rows: list[dict[str, Any]] = []
    text_by_key: dict[str, str] = {}
    entity_by_key: dict[str, Any] = {}
    evidence_by_key: dict[str, ProfileSnapshotEvidence | None] = {}
    professors = await repository.list_professors(session)
    academicians = (await session.execute(select(Academician).order_by(Academician.id.asc()))).scalars().all()
    for entity_type, entities in (("professor", professors), ("academician", academicians)):
        for entity in entities:
            entity_id = getattr(entity, "id", None)
            if entity_id is None:
                continue
            evidence = _snapshot_evidence_for_entity(
                snapshot_evidence,
                name=getattr(entity, "name", None),
                urls=[getattr(entity, "homepage", None), getattr(entity, "external_link", None), getattr(entity, "source_url", None)],
            )
            profile_text = _profile_text_for_llm(entity, evidence)
            if not profile_text:
                continue
            missing_fields = [
                field
                for field in ("bio", "research_areas", "title", "enrollment_pref")
                if _is_empty_text(getattr(entity, field, None))
            ]
            if not missing_fields:
                continue
            key = _entity_key(entity_type, entity_id)
            rows.append(
                {
                    "entity_key": key,
                    "entity_type": entity_type,
                    "name": getattr(entity, "name", None),
                    "org_unit_name": getattr(entity, "org_unit_name", None),
                    "missing_fields": missing_fields,
                    "current": _entity_snapshot(entity_type, entity),
                    "profile_text": profile_text,
                    "profile_source": _profile_evidence_payload(evidence),
                }
            )
            text_by_key[key] = profile_text
            entity_by_key[key] = entity
            evidence_by_key[key] = evidence
    return rows, text_by_key, entity_by_key, evidence_by_key


async def _apply_llm_identity_reviews(
    session: Any,
    *,
    run_id: int,
    db_name: str,
    apply: bool,
    snapshot_evidence: dict[str, list[ProfileSnapshotEvidence]],
    steward_llm: Any,
    max_context_tokens: int,
) -> tuple[int, set[str]]:
    rows, text_by_key, entity_by_key, evidence_by_key = await _identity_review_rows(session, snapshot_evidence)
    if not rows:
        return 0, set()
    decisions = await steward_llm.review_identities(rows, max_context_tokens)
    reviewed: set[str] = set()
    audits_written = 0
    for row in rows:
        key = str(row["entity_key"])
        decision = decisions.get(key)
        if not isinstance(decision, dict):
            continue
        entity = entity_by_key.get(key)
        if entity is None:
            continue
        action = str(decision.get("action") or "no_action").strip()
        confidence = _safe_confidence(decision.get("confidence"), default=0.0)
        spans = _evidence_spans_for_field(decision, None)
        evidence_ok = _evidence_spans_exist(spans, text_by_key.get(key, ""))
        reviewed.add(key)

        if row["entity_type"] == "professor" and action == "promote_academician":
            audits_written += await _audit_llm_identity_decision(
                session,
                run_id=run_id,
                db_name=db_name,
                entity_type="professor",
                entity=entity,
                issue_type="promoted_academician",
                action="promoted" if (apply and evidence_ok and confidence >= 0.90) else "report_only",
                decision=decision,
                evidence_ok=evidence_ok,
                spans=spans,
            )
            if apply and evidence_ok and confidence >= 0.90:
                await _mark_completion_recrawl_tasks_resolved(
                    session,
                    urls=[entity.homepage, entity.external_link],
                    evidence=evidence_by_key.get(key),
                )
                updates = decision.get("updates") if isinstance(decision.get("updates"), dict) else {}
                await crawler_db.upsert_academician_with_status(
                    session,
                    {
                        "name": entity.name,
                        "org_unit_name": entity.org_unit_name,
                        "title": "院士",
                        "research_areas": entity.research_areas or _clean_llm_text(updates.get("research_areas")),
                        "email": entity.email,
                        "phone": entity.phone,
                        "homepage": entity.homepage,
                        "external_link": entity.external_link,
                        "bio": entity.bio,
                        "enrollment_pref": entity.enrollment_pref,
                        "publications": entity.publications,
                        "source_url": entity.homepage,
                    },
                )
                await crawler_db.hard_delete_professor(session, entity)
            continue

        if row["entity_type"] == "academician" and action == "demote_to_professor":
            audits_written += await _audit_llm_identity_decision(
                session,
                run_id=run_id,
                db_name=db_name,
                entity_type="academician",
                entity=entity,
                issue_type="misclassified_academician",
                action="demoted_to_professor" if (apply and evidence_ok and confidence >= 0.90) else "report_only",
                decision=decision,
                evidence_ok=evidence_ok,
                spans=spans,
            )
            if apply and evidence_ok and confidence >= 0.90:
                updates = decision.get("updates") if isinstance(decision.get("updates"), dict) else {}
                demoted_title = _clean_llm_text(updates.get("title")) or _infer_non_academician_title_from_profile(
                    entity.bio,
                    evidence_by_key.get(key).text if evidence_by_key.get(key) is not None else None,
                )
                await crawler_db.upsert_professor_with_status(
                    session,
                    {
                        "name": entity.name,
                        "org_unit_id": entity.org_unit_id,
                        "title": demoted_title,
                        "research_areas": entity.research_areas,
                        "email": entity.email,
                        "phone": entity.phone,
                        "homepage": entity.homepage,
                        "external_link": entity.external_link,
                        "bio": entity.bio,
                        "enrollment_pref": entity.enrollment_pref,
                        "publications": entity.publications,
                        "source_url": entity.source_url or entity.homepage,
                    },
                )
                await session.delete(entity)
            continue

        if action in {"keep_professor", "keep_academician", "no_action"}:
            audits_written += await _audit_llm_identity_decision(
                session,
                run_id=run_id,
                db_name=db_name,
                entity_type=str(row["entity_type"]),
                entity=entity,
                issue_type="llm_identity_review",
                action="kept" if evidence_ok or action == "no_action" else "report_only",
                decision=decision,
                evidence_ok=evidence_ok,
                spans=spans,
            )
    if audits_written and apply:
        await session.flush()
    return audits_written, reviewed


async def _identity_review_rows(
    session: Any,
    snapshot_evidence: dict[str, list[ProfileSnapshotEvidence]],
) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, Any], dict[str, ProfileSnapshotEvidence | None]]:
    rows: list[dict[str, Any]] = []
    text_by_key: dict[str, str] = {}
    entity_by_key: dict[str, Any] = {}
    evidence_by_key: dict[str, ProfileSnapshotEvidence | None] = {}
    professors = await repository.list_professors(session)
    academicians = (await session.execute(select(Academician).order_by(Academician.id.asc()))).scalars().all()
    for professor in professors:
        if professor.id is None:
            continue
        evidence = _snapshot_evidence_for_entity(
            snapshot_evidence,
            name=professor.name,
            urls=[professor.homepage, professor.external_link],
        )
        profile_text = _profile_text_for_llm(professor, evidence)
        if not profile_text or not contains_academician_hint(professor.title, professor.bio, profile_text):
            continue
        key = _entity_key("professor", professor.id)
        rows.append(_identity_review_payload("professor", key, professor, evidence, profile_text))
        text_by_key[key] = profile_text
        entity_by_key[key] = professor
        evidence_by_key[key] = evidence
    for academician in academicians:
        if academician.id is None:
            continue
        evidence = _snapshot_evidence_for_entity(
            snapshot_evidence,
            name=academician.name,
            urls=[academician.homepage, academician.external_link, academician.source_url],
        )
        profile_text = _profile_text_for_llm(academician, evidence)
        if not profile_text:
            continue
        key = _entity_key("academician", academician.id)
        rows.append(_identity_review_payload("academician", key, academician, evidence, profile_text))
        text_by_key[key] = profile_text
        entity_by_key[key] = academician
        evidence_by_key[key] = evidence
    return rows, text_by_key, entity_by_key, evidence_by_key


def _identity_review_payload(
    entity_type: str,
    key: str,
    entity: Professor | Academician,
    evidence: ProfileSnapshotEvidence | None,
    profile_text: str,
) -> dict[str, Any]:
    return {
        "entity_key": key,
        "entity_type": entity_type,
        "current_entity_type": entity_type,
        "name": entity.name,
        "current": _entity_snapshot(entity_type, entity),
        "profile_text": profile_text,
        "name_window": evidence.name_window if evidence is not None else "",
        "profile_source": _profile_evidence_payload(evidence),
    }


async def _audit_llm_identity_decision(
    session: Any,
    *,
    run_id: int,
    db_name: str,
    entity_type: str,
    entity: Professor | Academician,
    issue_type: str,
    action: str,
    decision: dict[str, Any],
    evidence_ok: bool,
    spans: list[str],
) -> int:
    await repository.add_audit(
        session,
        run_id=run_id,
        db_name=db_name,
        entity_type=entity_type,
        entity_id=int(entity.id),
        issue_type=issue_type,
        reason=str(decision.get("reason") or "llm_identity_review"),
        confidence=_safe_confidence(decision.get("confidence"), default=0.0),
        evidence={
            "llm_action": str(decision.get("action") or "no_action"),
            "updates": decision.get("updates") if isinstance(decision.get("updates"), dict) else {},
            "evidence_spans": spans,
            "evidence_valid": evidence_ok,
            "recrawl_needed": bool(decision.get("recrawl_needed")),
            "skill_state": "STEWARD_IDENTITY_REVIEW",
        },
        action=action,
        before_snapshot=_entity_snapshot(entity_type, entity),
    )
    return 1


async def _demote_misclassified_academicians(
    session: Any,
    *,
    run_id: int,
    db_name: str,
    apply: bool,
    snapshot_evidence: dict[str, list[ProfileSnapshotEvidence]],
    identity_reviewed: set[str] | None = None,
) -> int:
    audits_written = 0
    reviewed = identity_reviewed or set()
    academicians = (await session.execute(select(Academician).order_by(Academician.id.asc()))).scalars().all()
    for academician in academicians:
        if _entity_key("academician", academician.id) in reviewed:
            continue
        evidence = _snapshot_evidence_for_entity(
            snapshot_evidence,
            name=academician.name,
            urls=[academician.homepage, academician.external_link, academician.source_url],
        )
        if not _has_profile_identity_evidence(academician.bio, evidence):
            continue
        if contains_self_academician_hint(academician.name, academician.bio):
            continue
        if evidence is not None and contains_self_academician_hint(academician.name, evidence.name_window, evidence.text):
            continue

        before_snapshot = _academician_snapshot(academician)
        demoted_title = _infer_non_academician_title_from_profile(
            academician.bio,
            evidence.text if evidence is not None else None,
        )
        await repository.add_audit(
            session,
            run_id=run_id,
            db_name=db_name,
            entity_type="academician",
            entity_id=academician.id,
            issue_type="misclassified_academician",
            reason=MISCLASSIFIED_ACADEMICIAN_REASON,
            confidence=0.90,
            evidence={
                "name": academician.name,
                "has_academician_mention": contains_academician_hint(
                    academician.bio,
                    evidence.text if evidence is not None else None,
                ),
                "demoted_title": demoted_title,
                **_profile_evidence_payload(evidence if evidence is not None else None),
            },
            action="demoted_to_professor" if apply else "report_only",
            before_snapshot=before_snapshot,
        )
        audits_written += 1
        if apply:
            await crawler_db.upsert_professor_with_status(
                session,
                {
                    "name": academician.name,
                    "org_unit_id": academician.org_unit_id,
                    "title": demoted_title,
                    "research_areas": academician.research_areas,
                    "email": academician.email,
                    "phone": academician.phone,
                    "homepage": academician.homepage,
                    "external_link": academician.external_link,
                    "bio": academician.bio,
                    "enrollment_pref": academician.enrollment_pref,
                    "publications": academician.publications,
                    "source_url": academician.source_url or academician.homepage,
                },
            )
            await session.delete(academician)
    if audits_written and apply:
        await session.flush()
    return audits_written


async def _load_profile_snapshot_evidence(session: Any) -> dict[str, list[ProfileSnapshotEvidence]]:
    rows = (
        await session.execute(
            select(CrawlTask)
            .where(
                CrawlTask.task_kind == CrawlTaskKind.DETAIL_PAGE.value,
                CrawlTask.page_text_snapshot != "",
            )
            .order_by(CrawlTask.id.asc())
        )
    ).scalars().all()
    by_url: dict[str, list[ProfileSnapshotEvidence]] = {}
    for task in rows:
        text = (task.page_text_snapshot or "").strip()
        if not text:
            continue
        evidence = ProfileSnapshotEvidence(
            task_id=int(task.id),
            source_url=task.source_url,
            page_url=task.page_url,
            text=text,
            name_window="",
        )
        for url in {task.source_url, task.page_url}:
            key = _normalize_url(url)
            if key:
                by_url.setdefault(key, []).append(evidence)
    for values in by_url.values():
        values.sort(key=lambda item: len(item.text), reverse=True)
    return by_url


def _snapshot_evidence_for_entity(
    snapshot_evidence: dict[str, list[ProfileSnapshotEvidence]],
    *,
    name: str | None,
    urls: list[str | None],
) -> ProfileSnapshotEvidence | None:
    clean_name = (name or "").strip()
    if not clean_name:
        return None
    seen_task_ids: set[int] = set()
    for url in urls:
        key = _normalize_url(url)
        if not key:
            continue
        for evidence in snapshot_evidence.get(key, []):
            if evidence.task_id in seen_task_ids:
                continue
            seen_task_ids.add(evidence.task_id)
            context = _snapshot_profile_context(evidence.text, clean_name)
            if not context:
                continue
            name_window = _snapshot_name_window(context, clean_name)
            return ProfileSnapshotEvidence(
                task_id=evidence.task_id,
                source_url=evidence.source_url,
                page_url=evidence.page_url,
                text=context,
                name_window=name_window,
            )
    return None


def _infer_research_from_profile_evidence(
    *,
    bio: str | None,
    snapshot_evidence: ProfileSnapshotEvidence | None,
) -> tuple[str | None, str, ProfileSnapshotEvidence | None]:
    inferred = infer_research_areas_from_bio(bio)
    if inferred:
        return inferred, BIO_INFERENCE_REASON, None
    if snapshot_evidence is not None:
        inferred = infer_research_areas_from_bio(snapshot_evidence.text)
        if inferred:
            return inferred, PROFILE_SNAPSHOT_INFERENCE_REASON, snapshot_evidence
    return None, "", None


def _has_profile_identity_evidence(bio: str | None, snapshot_evidence: ProfileSnapshotEvidence | None) -> bool:
    if (bio or "").strip():
        return True
    return bool(snapshot_evidence is not None and (snapshot_evidence.text or "").strip())


def _infer_non_academician_title_from_profile(*values: str | None) -> str | None:
    for value in values:
        text = (value or "").strip()
        if not text:
            continue
        for sentence in _profile_title_candidate_sentences(text):
            title = normalize_non_academician_title(sentence)
            if title:
                return title
    return None


def _profile_title_candidate_sentences(text: str) -> list[str]:
    sentences = [part.strip() for part in re.split(r"[。；;\n\r]+", text) if part.strip()]
    return [sentence for sentence in sentences if not _TITLE_RELATION_SENTENCE_RE.search(sentence)]


def _academician_reason_from_profile_evidence(
    *,
    name: str | None,
    title: str | None,
    bio: str | None,
    snapshot_evidence: ProfileSnapshotEvidence | None,
) -> str | None:
    if contains_self_academician_hint(name, title, bio):
        return BIO_ACADEMICIAN_REASON
    if snapshot_evidence is not None and contains_self_academician_hint(name, snapshot_evidence.name_window):
        return PROFILE_SNAPSHOT_ACADEMICIAN_REASON
    return None


def _snapshot_profile_context(text: str, name: str) -> str | None:
    index = text.find(name)
    if index < 0:
        return None
    start = max(0, index - 120)
    end_candidates = [
        pos
        for marker in ("[上一篇", "[下一篇", "友情链接", "版权所有", "地址：")
        if (pos := text.find(marker, index)) > index
    ]
    end = min(end_candidates) if end_candidates else min(len(text), index + 3500)
    return text[start:end].strip()


def _snapshot_name_window(text: str, name: str, *, radius: int = 700) -> str:
    index = text.find(name)
    if index < 0:
        return ""
    start = max(0, index - min(120, radius))
    end = min(len(text), index + len(name) + radius)
    return text[start:end].strip()


def _profile_evidence_payload(evidence: ProfileSnapshotEvidence | None) -> dict[str, Any]:
    if evidence is None:
        return {"text_source": "bio"}
    return {
        "text_source": "profile_snapshot",
        "crawl_task_id": evidence.task_id,
        "source_url": evidence.source_url,
        "page_url": evidence.page_url,
    }


def _profile_text_for_llm(entity: Professor | Academician, evidence: ProfileSnapshotEvidence | None) -> str:
    parts = []
    bio = (getattr(entity, "bio", None) or "").strip()
    if bio:
        parts.append(bio)
    if evidence is not None:
        if (evidence.name_window or "").strip():
            parts.append(evidence.name_window.strip())
        if (evidence.text or "").strip() and evidence.text.strip() not in parts:
            parts.append(evidence.text.strip())
    return "\n\n".join(part for part in parts if part).strip()


def _entity_key(entity_type: str, entity_id: Any) -> str:
    return f"{entity_type}:{int(entity_id)}"


def _entity_snapshot(entity_type: str, entity: Professor | Academician) -> dict[str, Any]:
    return _professor_snapshot(entity) if entity_type == "professor" else _academician_snapshot(entity)


def _is_empty_text(value: Any) -> bool:
    return not str(value or "").strip()


def _clean_llm_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _safe_confidence(value: Any, *, default: float) -> float:
    try:
        confidence = float(value)
    except Exception:
        confidence = default
    return max(0.0, min(confidence, 1.0))


def _evidence_spans_for_field(decision: dict[str, Any], field_name: str | None) -> list[str]:
    raw = decision.get("evidence_spans")
    if isinstance(raw, dict):
        if field_name is not None:
            return _coerce_spans(raw.get(field_name))
        spans: list[str] = []
        for value in raw.values():
            spans.extend(_coerce_spans(value))
        return spans
    return _coerce_spans(raw)


def _coerce_spans(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return []


def _evidence_spans_exist(spans: list[str], text: str) -> bool:
    haystack = str(text or "")
    if not spans or not haystack:
        return False
    return all(span in haystack for span in spans)


async def _mark_completion_recrawl_tasks_resolved(
    session: Any,
    *,
    urls: list[str | None],
    evidence: ProfileSnapshotEvidence | None,
) -> None:
    candidate_urls = {_normalize_url(url) for url in urls if _normalize_url(url)}
    task_ids = {evidence.task_id} if evidence is not None else set()
    filters = []
    if candidate_urls:
        filters.append(CrawlTask.source_url.in_(candidate_urls))
        filters.append(CrawlTask.page_url.in_(candidate_urls))
    if task_ids:
        filters.append(CrawlTask.id.in_(task_ids))
    if not filters:
        return
    tasks = (
        await session.execute(
            select(CrawlTask).where(
                CrawlTask.task_kind == CrawlTaskKind.DETAIL_PAGE.value,
                CrawlTask.status.in_([CrawlTaskStatus.PENDING.value, CrawlTaskStatus.RETRY.value]),
                CrawlTask.last_error.like("completion_recrawl_%"),
                or_(*filters),
            )
        )
    ).scalars().all()
    for task in tasks:
        task.status = CrawlTaskStatus.DONE.value
        task.last_error = COMPLETION_RECRAWL_REPAIRED_LAST_ERROR
        task.updated_at = _now_utc()
    if tasks:
        await session.flush()


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

    if homepage and (
        (professor.research_areas or "").strip() == ""
        or (professor.bio or "").strip() == ""
    ):
        evidence["recrawl_source"] = "homepage"
        return HOMEPAGE_PROFILE_INCOMPLETE_REASON, 0.95, evidence

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
    normalized_reason = (
        reason
        if reason in {"crawl_failure", "site_missing", "uncertain", HOMEPAGE_PROFILE_INCOMPLETE_REASON}
        else "uncertain"
    )
    normalized_confidence = max(0.0, min(float(confidence), 1.0))
    failure_count = int(evidence.get("failure_count") or 0)
    if normalized_reason == HOMEPAGE_PROFILE_INCOMPLETE_REASON and not evidence.get("homepage"):
        return "uncertain", min(normalized_confidence, 0.45)
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
        recrawl_field = _should_enqueue_recrawl_for_field(
            field_name=field_name,
            missing_fields=missing_fields,
            reason=reason,
        )
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
            action="recrawl_enqueued" if (apply and recrawl_field) else "report_only",
        )
        audits_written += 1
        missing_field_audits += 1

    if apply and _should_enqueue_recrawl(missing_fields=missing_fields, reason=reason):
        if await enqueue_recrawl_task(session, professor, reason=reason):
            recrawl_tasks_upserted += 1
    return audits_written, missing_field_audits, recrawl_tasks_upserted


async def resolve_source_url(session: Any, professor_id: int) -> str:
    return await repository.resolve_source_url(session, professor_id=professor_id)


def _should_enqueue_recrawl_for_field(*, field_name: str, missing_fields: list[str], reason: str) -> bool:
    if reason == HOMEPAGE_PROFILE_INCOMPLETE_REASON:
        return field_name in {"bio", "research_areas"}
    return reason == "crawl_failure" and bool(missing_fields)


def _should_enqueue_recrawl(*, missing_fields: list[str], reason: str) -> bool:
    if reason == HOMEPAGE_PROFILE_INCOMPLETE_REASON:
        return any(field in {"bio", "research_areas"} for field in missing_fields)
    return reason == "crawl_failure" and bool(missing_fields)


async def enqueue_recrawl_task(session: Any, professor: Professor, *, reason: str) -> bool:
    priority = 0
    last_error = "steward_recrawl_missing_core_fields"
    if reason == HOMEPAGE_PROFILE_INCOMPLETE_REASON:
        source_url = (professor.homepage or "").strip()
        priority = -10
        last_error = COMPLETION_RECRAWL_PROFILE_FIELDS_LAST_ERROR
    else:
        source_url = await resolve_source_url(session, professor.id)
        if not source_url:
            source_url = (professor.homepage or "").strip()
    if not source_url:
        return False
    return await repository.upsert_recrawl_task(
        session,
        professor=professor,
        source_url=source_url,
        last_error=last_error,
        priority=priority,
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


def _academician_snapshot(academician: Academician) -> dict[str, Any]:
    return {
        "id": academician.id,
        "name": academician.name,
        "org_unit_id": academician.org_unit_id,
        "title": academician.title,
        "research_areas": academician.research_areas,
        "email": academician.email,
        "phone": academician.phone,
        "homepage": academician.homepage,
        "external_link": academician.external_link,
        "bio": academician.bio,
        "enrollment_pref": academician.enrollment_pref,
        "publications": academician.publications,
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
