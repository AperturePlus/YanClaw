from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.crawler.fetchers import FetchResult


@dataclass
class CrawlSessionState:
    """Mutable per-agent state shared by crawler subsystems."""

    pipeline_enabled: bool
    visited_urls: set[str] = field(default_factory=set)
    fetch_cache: dict[str, FetchResult] = field(default_factory=dict)
    blocked_hosts: set[str] = field(default_factory=set)
    detail_visited_urls: set[str] = field(default_factory=set)
    detail_processed_by_org_unit: dict[str, int] = field(default_factory=dict)
    enriched_names_by_org_unit: dict[str, set[str]] = field(default_factory=dict)
    target_org_unit_ids: set[int] = field(default_factory=set)
    org_units_marked_no_faculty: set[int] = field(default_factory=set)
    pipeline_stats: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, *, pipeline_enabled: bool) -> "CrawlSessionState":
        state = cls(pipeline_enabled=bool(pipeline_enabled))
        state.pipeline_stats.update(
            {
                "enabled": bool(pipeline_enabled),
                "pending": 0,
                "in_progress": 0,
                "retry": 0,
                "done": 0,
                "failed": 0,
                "retries": 0,
                "invalid_json_failures": 0,
                "no_structured_data_failures": 0,
                "save_errors": 0,
                "processed_tasks": 0,
                "timed_tasks": 0,
                "average_task_ms": 0.0,
                "queue_depth": 0,
                "llm_calls_total": 0,
                "llm_calls_skipped_by_gate": 0,
                "llm_payload_bytes_total": 0,
                "avg_payload_bytes": 0.0,
                "followup_dropped_noise": 0,
                "list_enqueued": 0,
                "list_processed": 0,
                "list_failed": 0,
                "list_skipped": 0,
                "detail_enqueued": 0,
                "detail_processed": 0,
                "detail_failed": 0,
                "detail_skipped": 0,
                "followups_scheduled": 0,
                "pagination_scheduled": 0,
                "duplicate_tasks_skipped": 0,
                "duplicate_followups_skipped": 0,
                "detail_links_dropped_noise": 0,
                "detail_links_dropped_directory": 0,
                "detail_links_dropped_already_enriched": 0,
                "detail_pending_empty_with_candidates": 0,
            }
        )
        return state
