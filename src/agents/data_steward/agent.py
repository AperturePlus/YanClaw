from __future__ import annotations

from pathlib import Path

from agents.crawler.config import CrawlerSettings
from agents.data_steward.db import process_one_database, resolve_targets
from agents.data_steward.llm_service import DataStewardLLMService
from agents.data_steward.types import StewardBatchSummary
from runtime.database import DatabaseManager
from runtime.logger import get_logger


class DataStewardAgent:
    def __init__(self, *, settings: CrawlerSettings) -> None:
        self.settings = settings
        self.logger = get_logger("steward.agent")

    async def run(
        self,
        *,
        universities: list[str] | None,
        universities_file: Path | None,
        db_roots: list[str] | None,
        apply: bool,
        llm_enabled: bool,
        max_context_tokens: int,
        include_backup_audit: bool,
    ) -> StewardBatchSummary:
        resolution = resolve_targets(
            settings=self.settings,
            universities=universities,
            universities_file=universities_file,
            db_roots=db_roots,
        )
        mode = "apply" if apply else "dry_run"
        steward_llm_factory = self._build_llm_service if llm_enabled and self.settings.openai_api_key else None
        summaries = []
        target_count = len(resolution.targets)
        self.logger.info(
            "Data Steward batch start mode=%s targets=%s llm_enabled=%s include_backup_audit=%s",
            mode,
            target_count,
            steward_llm_factory is not None,
            include_backup_audit,
        )
        for index, target_db in enumerate(resolution.targets, start=1):
            self.logger.info(
                "Data Steward target start index=%s/%s db=%s",
                index,
                target_count,
                target_db,
            )
            summary = await process_one_database(
                settings=self.settings,
                target_db=target_db,
                mode=mode,
                max_context_tokens=max_context_tokens,
                include_backup_audit=include_backup_audit,
                steward_llm_factory=steward_llm_factory,
            )
            summaries.append(summary)
            self.logger.info(
                "Data Steward target done index=%s/%s db=%s status=%s duplicates=%s deleted=%s "
                "excluded_org_units=%s excluded_org_units_deleted=%s sub_department_sections=%s "
                "sub_department_sections_merged=%s missing_audits=%s recrawl_tasks=%s audits_written=%s",
                index,
                target_count,
                target_db,
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

        batch_summary = StewardBatchSummary(
            mode=mode,
            targets=[str(path) for path in resolution.targets],
            total_duplicates_detected=sum(item.duplicates_detected for item in summaries),
            total_duplicates_deleted=sum(item.duplicates_deleted for item in summaries),
            total_missing_field_audits=sum(item.missing_field_audits for item in summaries),
            total_recrawl_tasks_upserted=sum(item.recrawl_tasks_upserted for item in summaries),
            total_audits_written=sum(item.audits_written for item in summaries),
            unmatched_universities=resolution.unmatched_universities,
            unmatched_db_roots=resolution.unmatched_db_roots,
            runs=summaries,
            total_excluded_org_units_detected=sum(item.excluded_org_units_detected for item in summaries),
            total_excluded_org_units_deleted=sum(item.excluded_org_units_deleted for item in summaries),
            total_sub_department_sections_detected=sum(
                item.sub_department_sections_detected for item in summaries
            ),
            total_sub_department_sections_merged=sum(item.sub_department_sections_merged for item in summaries),
        )
        self.logger.info(
            "Data Steward batch done mode=%s targets=%s duplicates=%s deleted=%s "
            "excluded_org_units=%s excluded_org_units_deleted=%s sub_department_sections=%s "
            "sub_department_sections_merged=%s missing_audits=%s recrawl_tasks=%s audits_written=%s "
            "unmatched_universities=%s unmatched_db_roots=%s",
            batch_summary.mode,
            len(batch_summary.targets),
            batch_summary.total_duplicates_detected,
            batch_summary.total_duplicates_deleted,
            batch_summary.total_excluded_org_units_detected,
            batch_summary.total_excluded_org_units_deleted,
            batch_summary.total_sub_department_sections_detected,
            batch_summary.total_sub_department_sections_merged,
            batch_summary.total_missing_field_audits,
            batch_summary.total_recrawl_tasks_upserted,
            batch_summary.total_audits_written,
            len(batch_summary.unmatched_universities),
            len(batch_summary.unmatched_db_roots),
        )
        return batch_summary

    def _build_llm_service(self, db: DatabaseManager) -> DataStewardLLMService:
        return DataStewardLLMService(settings=self.settings, db=db)

