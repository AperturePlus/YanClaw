"""Compatibility facade for crawler DB operations.

This package re-exports stable public functions while the implementation
lives in smaller focused modules.
"""

from agents.crawler.db.professors import (
    ensure_professor_affiliation,
    upsert_academician,
    upsert_professor,
)
from agents.crawler.db.schema import ensure_runtime_schema
from agents.crawler.db.tasks import (
    list_recoverable_crawl_tasks,
    log_extraction_failure,
    set_crawl_task_status,
    summarize_crawl_task_status,
    upsert_crawl_task,
)
from agents.crawler.db.university import (
    count_academicians,
    count_professors,
    ensure_university_meta,
    get_or_create_org_unit,
    get_university_status,
    is_url_crawled,
    list_org_units,
    load_university_targets_from_csv,
    log_crawl,
    set_university_status,
)

__all__ = [
    "count_academicians",
    "count_professors",
    "ensure_professor_affiliation",
    "ensure_runtime_schema",
    "ensure_university_meta",
    "get_or_create_org_unit",
    "get_university_status",
    "is_url_crawled",
    "list_org_units",
    "list_recoverable_crawl_tasks",
    "load_university_targets_from_csv",
    "log_crawl",
    "log_extraction_failure",
    "set_crawl_task_status",
    "set_university_status",
    "summarize_crawl_task_status",
    "upsert_academician",
    "upsert_crawl_task",
    "upsert_professor",
]
