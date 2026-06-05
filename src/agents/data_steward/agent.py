from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agents.crawler.config import CrawlerSettings
from agents.data_steward.db import process_one_database, resolve_targets
from agents.data_steward.types import StewardBatchSummary
from runtime.context import ContextManager
from runtime.llm import LLMClient


class DataStewardAgent:
    def __init__(self, *, settings: CrawlerSettings) -> None:
        self.settings = settings
        self.context_manager = ContextManager(settings.openai_model)

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
        classifier = self._classify_uncertain_with_llm if llm_enabled else None
        summaries = []
        for target_db in resolution.targets:
            summary = await process_one_database(
                settings=self.settings,
                target_db=target_db,
                mode=mode,
                max_context_tokens=max_context_tokens,
                include_backup_audit=include_backup_audit,
                uncertain_classifier=classifier,
            )
            summaries.append(summary)

        return StewardBatchSummary(
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

    async def _classify_uncertain_with_llm(
        self,
        rows: list[dict[str, Any]],
        max_context_tokens: int,
    ) -> dict[int, dict[str, Any]]:
        if not rows:
            return {}
        if not self.settings.openai_api_key:
            return {}

        budget = max(4096, min(int(max_context_tokens), 256000))
        llm = LLMClient(
            self.settings.openai_base_url,
            self.settings.openai_api_key,
            self.settings.openai_model,
            timeout_seconds=self.settings.llm_timeout_seconds,
            max_rounds=1,
        )
        result: dict[int, dict[str, Any]] = {}
        batches = self._chunk_rows_for_llm(rows, budget)
        for batch in batches:
            payload = json.dumps({"items": batch}, ensure_ascii=False)
            response = await llm.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            "You classify missing faculty fields. "
                            "Allowed reasons: crawl_failure, site_missing, uncertain. "
                            "Return strict JSON object: {\"items\":[{\"id\":<int>,\"reason\":\"...\",\"confidence\":<0..1>}]}."
                        ),
                    },
                    {"role": "user", "content": payload},
                ]
            )
            parsed = self._parse_json_object(response.content)
            items = parsed.get("items") if isinstance(parsed, dict) else None
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                try:
                    row_id = int(item.get("id"))
                except Exception:
                    continue
                reason = str(item.get("reason") or "uncertain").strip()
                confidence = float(item.get("confidence") or 0.35)
                result[row_id] = {"reason": reason, "confidence": confidence}
        return result

    def _chunk_rows_for_llm(self, rows: list[dict[str, Any]], budget: int) -> list[list[dict[str, Any]]]:
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for row in rows:
            candidate = current + [row]
            content = json.dumps({"items": candidate}, ensure_ascii=False)
            if current and self.context_manager.count_tokens(content) > budget:
                chunks.append(current)
                current = [row]
                continue
            current = candidate
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _parse_json_object(text: str) -> dict[str, Any]:
        raw = (text or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(raw[start : end + 1])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {}
        return {}

