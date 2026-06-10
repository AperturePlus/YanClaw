from __future__ import annotations

import re
from typing import Any

from agents.crawler import agent_detail
from agents.crawler.db.professors import normalize_professor_homepage
from agents.crawler.extraction_models import ExtractionTaskItem as _ExtractionTaskItem
from agents.crawler.models import CrawlTaskKind
from agents.crawler.org_unit_filter import (
    is_teaching_experiment_center_name,
    looks_like_sub_department_section_name,
)
from agents.crawler.sanitizer import (
    contains_academician_hint,
    contains_self_academician_hint,
    normalize_name,
    normalize_name_key,
    sanitize_professor_payload,
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
        if not self._is_detail_extraction_task(task):
            self._record_list_payload_suppressed(payload, task=task)
            return None

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
            return self._finalize_professor_payload_for_task(normalized, task=task)

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
            return self._finalize_professor_payload_for_task(normalized, task=task)

        normalized["org_unit_name"] = effective_name
        if not normalized.get("org_unit_url"):
            normalized["org_unit_url"] = task.org_unit_url
        if not normalized.get("source_url"):
            normalized["source_url"] = task.source_url
        self._fill_missing_homepages_from_name_links(normalized, task=task)
        self._infer_academician_flags_from_detail_context(normalized, task=task)
        return self._finalize_professor_payload_for_task(normalized, task=task)

    @staticmethod
    def _is_detail_extraction_task(task: _ExtractionTaskItem) -> bool:
        return bool(getattr(task, "detail_mode", False)) or (
            str(getattr(task, "task_kind", "") or "") == CrawlTaskKind.DETAIL_PAGE.value
        )

    def _record_list_payload_suppressed(self, payload: dict[str, Any], *, task: _ExtractionTaskItem) -> None:
        professors = payload.get("professors")
        record_count = len(professors) if isinstance(professors, list) else 0
        self._pipeline_stats["list_payloads_suppressed"] = int(
            self._pipeline_stats.get("list_payloads_suppressed", 0)
        ) + 1
        self._pipeline_stats["list_records_suppressed"] = int(
            self._pipeline_stats.get("list_records_suppressed", 0)
        ) + record_count
        self.logger.info(
            "Drop list-page professor payload task_id=%s org_unit=%s source=%s records=%s",
            getattr(task, "task_id", 0),
            getattr(task, "org_unit_name", "Unknown"),
            payload.get("source_url") or getattr(task, "source_url", ""),
            record_count,
        )

    def _finalize_professor_payload_for_task(
        self,
        payload: dict[str, Any],
        *,
        task: _ExtractionTaskItem,
    ) -> dict[str, Any] | None:
        filtered = self._filter_detail_professors_for_evidence(payload, task=task)
        if filtered is None:
            return None
        payload["professors"] = filtered
        return payload

    def _filter_detail_professors_for_evidence(
        self,
        payload: dict[str, Any],
        *,
        task: _ExtractionTaskItem,
    ) -> list[dict[str, Any]] | None:
        professors = payload.get("professors")
        if not isinstance(professors, list):
            self._pipeline_stats["detail_payloads_missing_professors"] = int(
                self._pipeline_stats.get("detail_payloads_missing_professors", 0)
            ) + 1
            return None

        org_unit_name = str(payload.get("org_unit_name") or task.org_unit_name or "Unknown")
        accepted: list[tuple[int, int, dict[str, Any]]] = []
        dropped_low_evidence = 0
        dropped_invalid = 0
        for index, professor in enumerate(professors):
            if not isinstance(professor, dict):
                dropped_invalid += 1
                continue
            try:
                cleaned, is_academician = sanitize_professor_payload(
                    professor,
                    org_unit_name=org_unit_name,
                )
            except Exception as exc:
                dropped_invalid += 1
                self.logger.debug(
                    "Drop invalid detail professor payload task_id=%s source=%s index=%s error=%s",
                    getattr(task, "task_id", 0),
                    getattr(task, "source_url", ""),
                    index,
                    exc,
                )
                continue

            evidence_fields = self._detail_professor_evidence_fields(professor, cleaned)
            if not evidence_fields:
                dropped_low_evidence += 1
                self.logger.debug(
                    "Drop low-evidence detail professor payload task_id=%s source=%s name=%s",
                    getattr(task, "task_id", 0),
                    getattr(task, "source_url", ""),
                    cleaned.get("name"),
                )
                continue

            updated = dict(professor)
            for key, value in cleaned.items():
                updated[key] = value
            if is_academician:
                updated["is_academician"] = True
            if professor.get("_self_academician_evidence") is True:
                updated["_self_academician_evidence"] = True
            accepted.append((self._detail_professor_evidence_score(updated, cleaned), index, updated))

        if dropped_invalid:
            self._pipeline_stats["detail_records_dropped_invalid"] = int(
                self._pipeline_stats.get("detail_records_dropped_invalid", 0)
            ) + dropped_invalid
        if dropped_low_evidence:
            self._pipeline_stats["detail_records_dropped_low_evidence"] = int(
                self._pipeline_stats.get("detail_records_dropped_low_evidence", 0)
            ) + dropped_low_evidence
        if not accepted:
            self._pipeline_stats["detail_payloads_dropped_no_evidence"] = int(
                self._pipeline_stats.get("detail_payloads_dropped_no_evidence", 0)
            ) + 1
            return None

        if len(accepted) > 1 and not self._detail_task_allows_multiple_professors(task):
            accepted.sort(key=lambda item: (-item[0], item[1]))
            suppressed = len(accepted) - 1
            self._pipeline_stats["detail_multi_professor_suppressed"] = int(
                self._pipeline_stats.get("detail_multi_professor_suppressed", 0)
            ) + suppressed
            self.logger.info(
                "Suppress extra professors on single-profile detail task_id=%s source=%s kept=%s suppressed=%s",
                getattr(task, "task_id", 0),
                getattr(task, "source_url", ""),
                accepted[0][2].get("name"),
                suppressed,
            )
            return [accepted[0][2]]

        accepted.sort(key=lambda item: item[1])
        return [item[2] for item in accepted]

    def _detail_professor_evidence_fields(
        self,
        raw: dict[str, Any],
        cleaned: dict[str, Any],
    ) -> set[str]:
        fields: set[str] = set()
        for field_name in (
            "title",
            "email",
            "phone",
            "research_areas",
            "bio",
            "publications",
            "enrollment_pref",
        ):
            if self._has_profile_value(cleaned.get(field_name)):
                fields.add(field_name)

        homepage = normalize_professor_homepage(raw.get("homepage") or cleaned.get("homepage"))
        if homepage:
            fields.add("homepage")
        if self._has_profile_value(raw.get("external_link") or cleaned.get("external_link")):
            fields.add("external_link")
        return fields

    def _detail_professor_evidence_score(self, raw: dict[str, Any], cleaned: dict[str, Any]) -> int:
        fields = self._detail_professor_evidence_fields(raw, cleaned)
        weights = {
            "title": 5,
            "email": 6,
            "phone": 4,
            "research_areas": 5,
            "bio": 5,
            "homepage": 3,
            "external_link": 2,
            "publications": 2,
            "enrollment_pref": 2,
        }
        score = sum(weights.get(field_name, 1) for field_name in fields)
        if raw.get("is_academician") is True:
            score += 3
        return score

    def _detail_task_allows_multiple_professors(self, task: _ExtractionTaskItem) -> bool:
        text = str(getattr(task, "page_text_snapshot", "") or "")
        url = str(getattr(task, "page_url", None) or getattr(task, "source_url", "") or "").lower()
        if any(token in url for token in ("yuanshi", "academician", "yuan-shi", "lyys")):
            return True
        aggregate_hints = (
            "团队成员",
            "团队介绍",
            "课题组成员",
            "研究团队",
            "教师团队",
            "院士风采",
            "院士名录",
            "院士列表",
            "两院院士",
            "院士团队",
            "team members",
            "research team",
        )
        lowered_text = text.lower()
        return any(hint in text or hint in lowered_text for hint in aggregate_hints)

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

