from __future__ import annotations

import re
from typing import Any

from agents.crawler import agent_detail
from agents.crawler.extraction_models import ExtractionTaskItem as _ExtractionTaskItem
from agents.crawler.org_unit_filter import (
    is_teaching_experiment_center_name,
    looks_like_sub_department_section_name,
)
from agents.crawler.sanitizer import (
    contains_academician_hint,
    contains_self_academician_hint,
    normalize_name,
    normalize_name_key,
)


class ExtractionPayloadService:
    """Payload normalization and profile-field fallback helpers for extraction tasks."""

    def _apply_detail_snapshot_profile_fallback(
        self,
        payloads: list[dict[str, Any]],
        task: _ExtractionTaskItem,
    ) -> bool:
        if not task.detail_mode:
            return False
        record = agent_detail.extract_detail_profile_record_from_snapshot(
            task.page_text_snapshot,
            page_url=task.page_url or task.source_url,
        )
        if not record:
            return False

        if payloads:
            changed = self._fill_payloads_from_detail_snapshot_record(payloads, record)
            if changed:
                self._pipeline_stats["detail_snapshot_fields_filled"] = int(
                    self._pipeline_stats.get("detail_snapshot_fields_filled", 0)
                ) + changed
                self.logger.info(
                    "Filled missing detail fields from snapshot task_id=%s url=%s name=%s fields=%s",
                    task.task_id,
                    task.source_url,
                    record.get("name"),
                    changed,
                )
            return changed > 0

        normalized_payload = self._normalize_extraction_payload_for_task(
            {
                "org_unit_name": task.org_unit_name,
                "org_unit_url": task.org_unit_url,
                "source_url": task.source_url,
                "professors": [record],
            },
            task=task,
        )
        if normalized_payload is None:
            return False
        payloads.append(normalized_payload)
        self._pipeline_stats["detail_snapshot_payloads_synthesized"] = int(
            self._pipeline_stats.get("detail_snapshot_payloads_synthesized", 0)
        ) + 1
        self.logger.info(
            "Synthesized detail payload from snapshot task_id=%s url=%s name=%s",
            task.task_id,
            task.source_url,
            record.get("name"),
        )
        return True

    def _fill_payloads_from_detail_snapshot_record(
        self,
        payloads: list[dict[str, Any]],
        record: dict[str, Any],
    ) -> int:
        snapshot_name = normalize_name(record.get("name"))
        if not snapshot_name:
            return 0
        fillable_fields = (
            "title",
            "email",
            "phone",
            "homepage",
            "external_link",
            "research_areas",
            "bio",
        )
        changed = 0
        for payload in payloads:
            professors = payload.get("professors")
            if not isinstance(professors, list):
                continue
            for index, professor in enumerate(professors):
                if not isinstance(professor, dict):
                    continue
                if normalize_name(professor.get("name")) != snapshot_name:
                    continue
                updated = dict(professor)
                for field_name in fillable_fields:
                    if self._has_profile_value(updated.get(field_name)):
                        continue
                    value = record.get(field_name)
                    if not self._has_profile_value(value):
                        continue
                    updated[field_name] = value
                    changed += 1
                if record.get("is_academician") is True and updated.get("is_academician") is not True:
                    updated["is_academician"] = True
                    changed += 1
                if (
                    record.get("_self_academician_evidence") is True
                    and updated.get("_self_academician_evidence") is not True
                ):
                    updated["_self_academician_evidence"] = True
                    changed += 1
                professors[index] = updated
        return changed

    @staticmethod
    def _has_profile_value(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, set, dict)):
            return bool(value)
        return True

    def _normalize_extraction_payload_for_task(
        self,
        payload: dict[str, Any],
        *,
        task: _ExtractionTaskItem,
    ) -> dict[str, Any] | None:
        incoming_name = str(payload.get("org_unit_name") or "").strip()
        task_name = str(task.org_unit_name or "").strip()
        effective_name = incoming_name or task_name or "Unknown"
        if is_teaching_experiment_center_name(effective_name):
            self._pipeline_stats["teaching_center_payloads_dropped"] = int(
                self._pipeline_stats.get("teaching_center_payloads_dropped", 0)
            ) + 1
            self.logger.info(
                "Drop professor payload for teaching/experiment center task_org=%s payload_org=%s source=%s",
                task_name,
                incoming_name,
                payload.get("source_url") or task.source_url,
            )
            return None

        normalized = dict(payload)
        if (
            incoming_name
            and task_name
            and task_name != "Unknown"
            and incoming_name != task_name
            and self._is_alias_for_org_unit(task_name, incoming_name)
        ):
            normalized["org_unit_name"] = task_name
            normalized["org_unit_url"] = task.org_unit_url or normalized.get("org_unit_url")
            if not normalized.get("source_url"):
                normalized["source_url"] = task.source_url
            self._pipeline_stats["alias_payloads_rewritten"] = int(
                self._pipeline_stats.get("alias_payloads_rewritten", 0)
            ) + 1
            self.logger.info(
                "Rewrite alias professor payload to canonical org_unit canonical=%s alias=%s source=%s",
                task_name,
                incoming_name,
                normalized.get("source_url") or task.source_url,
            )
            self._fill_missing_homepages_from_name_links(normalized, task=task)
            self._infer_academician_flags_from_detail_context(normalized, task=task)
            return normalized

        if (
            incoming_name
            and task_name
            and task_name != "Unknown"
            and incoming_name != task_name
            and looks_like_sub_department_section_name(incoming_name)
        ):
            normalized["org_unit_name"] = task_name
            normalized["org_unit_url"] = task.org_unit_url or normalized.get("org_unit_url")
            if not normalized.get("source_url"):
                normalized["source_url"] = task.source_url
            self._pipeline_stats["sub_department_payloads_rewritten"] = int(
                self._pipeline_stats.get("sub_department_payloads_rewritten", 0)
            ) + 1
            self.logger.info(
                "Rewrite sub-department professor payload to parent org_unit parent=%s child=%s source=%s",
                task_name,
                incoming_name,
                normalized.get("source_url") or task.source_url,
            )
            self._fill_missing_homepages_from_name_links(normalized, task=task)
            self._infer_academician_flags_from_detail_context(normalized, task=task)
            return normalized

        normalized["org_unit_name"] = effective_name
        if not normalized.get("org_unit_url"):
            normalized["org_unit_url"] = task.org_unit_url
        if not normalized.get("source_url"):
            normalized["source_url"] = task.source_url
        self._fill_missing_homepages_from_name_links(normalized, task=task)
        self._infer_academician_flags_from_detail_context(normalized, task=task)
        return normalized

    def _is_alias_for_org_unit(self, canonical_name: str, value: str) -> bool:
        canonical_key = self._normalize_org_unit_match_text(canonical_name)
        value_key = self._normalize_org_unit_match_text(value)
        if not canonical_key or not value_key:
            return False
        return value_key in self._manual_org_unit_aliases_by_name.get(canonical_key, set())

    def _fill_missing_homepages_from_name_links(
        self,
        payload: dict[str, Any],
        *,
        task: _ExtractionTaskItem,
    ) -> int:
        name_homepage_candidates = getattr(task, "name_homepage_candidates", None) or {}
        if getattr(task, "detail_mode", False) or not name_homepage_candidates:
            return 0
        professors = payload.get("professors")
        if not isinstance(professors, list):
            return 0
        changed = 0
        for index, professor in enumerate(professors):
            if not isinstance(professor, dict):
                continue
            if self._has_profile_value(professor.get("homepage")):
                continue
            name_key = normalize_name_key(professor.get("name"))
            if not name_key:
                continue
            homepage = name_homepage_candidates.get(name_key)
            if not homepage:
                continue
            updated = dict(professor)
            updated["homepage"] = homepage
            professors[index] = updated
            changed += 1
        if changed:
            self._pipeline_stats["homepage_filled_from_list_links"] = int(
                self._pipeline_stats.get("homepage_filled_from_list_links", 0)
            ) + changed
            self.logger.debug(
                "Filled %s missing professor homepages from list links task_id=%s org_unit=%s source=%s",
                changed,
                task.task_id,
                task.org_unit_name,
                task.source_url,
            )
        return changed

    def _infer_academician_flags_from_detail_context(
        self,
        payload: dict[str, Any],
        *,
        task: _ExtractionTaskItem,
    ) -> None:
        if not task.detail_mode:
            return
        page_text = str(task.page_text_snapshot or "")
        if not page_text or not contains_academician_hint(page_text):
            return
        professors = payload.get("professors")
        if not isinstance(professors, list):
            return
        changed = 0
        for index, professor in enumerate(professors):
            if not isinstance(professor, dict):
                continue
            if contains_self_academician_hint(
                professor.get("name"),
                professor.get("title"),
                professor.get("bio"),
            ):
                continue
            name = normalize_name(professor.get("name"))
            if not name:
                continue
            window = self._name_context_window(page_text, name)
            if not window or not contains_self_academician_hint(name, window):
                continue
            updated = dict(professor)
            updated["is_academician"] = True
            updated["_self_academician_evidence"] = True
            professors[index] = updated
            changed += 1
        if changed:
            self._pipeline_stats["academician_flags_inferred_from_detail"] = int(
                self._pipeline_stats.get("academician_flags_inferred_from_detail", 0)
            ) + changed

    @staticmethod
    def _name_context_window(text: str, name: str, *, radius: int = 160) -> str:
        if not text or not name:
            return ""
        index = text.find(name)
        if index < 0:
            return ""
        start = max(0, index - radius)
        end = min(len(text), index + len(name) + radius)
        return text[start:end]

