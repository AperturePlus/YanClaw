from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agents.crawler.models import CrawlTaskKind


@dataclass(frozen=True)
class QueuedUrl:
    url: str
    depth: int
    label: str = ""
    org_unit_id: int | None = None
    fetch_action: dict[str, Any] | None = None
    identity_url: str | None = None
    graph_node_id: int | None = None
    graph_node_type: str = ""
    graph_priority_score: float = 0.0

    @property
    def queue_url(self) -> str:
        return self.identity_url or self.url


@dataclass
class ExtractionTaskItem:
    task_id: int
    university: str
    org_unit_name: str
    org_unit_url: str | None
    source_url: str
    page_url: str
    page_hash: str
    page_text_snapshot: str
    allowed_tools: list[str]
    attempt: int = 0
    priority: int = 0
    strict_retry: bool = False
    detail_mode: bool = False
    task_kind: str = CrawlTaskKind.LIST_PAGE.value
    recovered: bool = False
    name_homepage_candidates: dict[str, str] = field(default_factory=dict)
    graph_node_id: int | None = None


@dataclass
class SaveEvent:
    task: ExtractionTaskItem
    payloads: list[dict[str, Any]]


@dataclass
class ExtractionOutcome:
    payloads: list[dict[str, Any]]
    invalid_json_events: list[dict[str, Any]]
    content_fallback_used: bool = False
    skipped_by_gate: bool = False
    skip_reason: str = ""
