"""Compatibility facade for crawler DB operations.

This package re-exports stable public functions while the implementation
lives in smaller focused modules.
"""

from agents.crawler.db.professors import (
    UpsertEntityResult,
    ensure_professor_affiliation,
    upsert_academician,
    upsert_academician_with_status,
    upsert_professor,
    upsert_professor_with_status,
)
from agents.crawler.db.schema import ensure_runtime_schema
from agents.crawler.db.steward import (
    add_data_quality_audit,
    create_steward_run,
    delete_professor_duplicates_for_academician,
    finish_steward_run,
    hard_delete_professor,
    list_professor_academician_duplicates,
    match_academician_for_professor,
    merge_into_academician_from_professor,
)
from agents.crawler.db.tasks import (
    list_recoverable_crawl_tasks,
    log_extraction_failure,
    recover_stale_in_progress_crawl_tasks,
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
    set_org_unit_status,
    set_university_status,
)

__all__ = [
    "count_academicians",
    "count_professors",
    "create_steward_run",
    "delete_professor_duplicates_for_academician",
    "finish_steward_run",
    "add_data_quality_audit",
    "ensure_professor_affiliation",
    "ensure_runtime_schema",
    "ensure_university_meta",
    "get_or_create_org_unit",
    "get_university_status",
    "is_url_crawled",
    "list_org_units",
    "list_professor_academician_duplicates",
    "list_recoverable_crawl_tasks",
    "load_university_targets_from_csv",
    "log_crawl",
    "log_extraction_failure",
    "match_academician_for_professor",
    "merge_into_academician_from_professor",
    "recover_stale_in_progress_crawl_tasks",
    "hard_delete_professor",
    "UpsertEntityResult",
    "set_crawl_task_status",
    "set_org_unit_status",
    "set_university_status",
    "summarize_crawl_task_status",
    "upsert_academician",
    "upsert_academician_with_status",
    "upsert_crawl_task",
    "upsert_professor",
    "upsert_professor_with_status",
]
