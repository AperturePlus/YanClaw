from __future__ import annotations

from typing import Any

from agents.crawler.models import CrawlTaskKind


class ExtractionPipeline:
    """Shared extraction-pipeline accounting helpers."""

    def __init__(self, stats: dict[str, Any]) -> None:
        self.stats = stats

    @staticmethod
    def task_kind_prefix(task: Any) -> str:
        if getattr(task, "task_kind", "") == CrawlTaskKind.DETAIL_PAGE.value or getattr(task, "detail_mode", False):
            return "detail"
        return "list"

    def increment_task_kind_stat(self, task: Any, suffix: str, amount: int = 1) -> None:
        key = f"{self.task_kind_prefix(task)}_{suffix}"
        self.stats[key] = int(self.stats.get(key, 0)) + int(amount)

    def update_timing(self, elapsed_ms: float) -> None:
        total = float(self.stats.get("average_task_ms", 0.0))
        timed = int(self.stats.get("timed_tasks", 0))
        current = timed + 1
        self.stats["average_task_ms"] = ((total * timed) + elapsed_ms) / current
        self.stats["timed_tasks"] = current

    def record_llm_payload(self, payload_bytes: int) -> None:
        calls = int(self.stats.get("llm_calls_total", 0)) + 1
        total = int(self.stats.get("llm_payload_bytes_total", 0)) + max(0, int(payload_bytes))
        self.stats["llm_calls_total"] = calls
        self.stats["llm_payload_bytes_total"] = total
        self.stats["avg_payload_bytes"] = float(total) / float(calls)
