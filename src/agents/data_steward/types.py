from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StewardRunSummary:
    db_name: str
    mode: str
    status: str
    duplicates_detected: int
    duplicates_deleted: int
    missing_field_audits: int
    recrawl_tasks_upserted: int
    audits_written: int
    excluded_org_units_detected: int = 0
    excluded_org_units_deleted: int = 0
    sub_department_sections_detected: int = 0
    sub_department_sections_merged: int = 0
    org_unit_cleanup: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    backup_audit: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StewardBatchSummary:
    mode: str
    targets: list[str]
    total_duplicates_detected: int
    total_duplicates_deleted: int
    total_missing_field_audits: int
    total_recrawl_tasks_upserted: int
    total_audits_written: int
    unmatched_universities: list[str]
    unmatched_db_roots: list[str]
    runs: list[StewardRunSummary]
    total_excluded_org_units_detected: int = 0
    total_excluded_org_units_deleted: int = 0
    total_sub_department_sections_detected: int = 0
    total_sub_department_sections_merged: int = 0


@dataclass(frozen=True)
class TargetResolution:
    targets: list[Path]
    unmatched_universities: list[str]
    unmatched_db_roots: list[str]

