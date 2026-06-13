"""One-time backfill for SJTU Ocean College (海洋学院) faculty profiles.

The 2026-06-12 run stored every soo profile URL as a traversal-only ``list_page``
task because the shared detail matcher did not recognize SJTU Ocean's hyphenated
``teacher-list/<slug-or-id>.html`` buckets, so 0 professors were saved even though
each task's ``page_text_snapshot`` already holds the full profile (name, title,
email, research areas, publications).

This script replays those stored snapshots through the normal professor upsert path
(reusing the generic helpers from ``repair_sjtu_mechanical``), then marks each task as
a completed detail task and retargets its graph node to a DETAIL_URL node. No re-crawl
is required for the captured profiles. Postdoc roster pages (``teacher-list2/*`` =
博士后) are intentionally excluded; profile-shaped pages whose snapshots do not yield a
record are reopened as detail tasks for the next resume to refetch under the fixed
matcher + content rescue.

Dry-run by default. Use --apply to write changes (an automatic backup is taken first).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.crawler import agent_detail  # noqa: E402
from agents.crawler import db as crawler_db  # noqa: E402
from agents.crawler.db.utils import _normalize_url  # noqa: E402
from agents.crawler.models import (  # noqa: E402
    CrawlGraphNodeStatus,
    CrawlTask,
)
from runtime.database import DatabaseManager  # noqa: E402

# Reuse the generic (non-SJTU-ME-specific) repair machinery.
from repair_sjtu_mechanical import (  # noqa: E402
    RepairSummary,
    _backup_db,
    _ensure_detail_graph_node,
    _load_org_unit,
    _mark_task_replayed,
    _reopen_task_for_refetch,
    _replay_record,
    _sqlite_url,
)


DEFAULT_DB = ROOT / "data" / "universities" / "sjtu.edu.cn.db"
DEFAULT_ORG_UNIT = "海洋学院"
# 教师名录 profiles live at /teacher-list/<slug-or-id>.html. teacher-list2 (博士后) and
# the research-news/project buckets (scient-list, scientific-*, base-list) are excluded.
_SOO_PROFILE_RE = re.compile(r"^/teacher-list/[a-z0-9][a-z0-9_]*\.s?html?$", re.IGNORECASE)


def _is_soo_profile_url(url: str) -> bool:
    normalized = _normalize_url(url)
    if not normalized:
        return False
    parsed = urlparse(normalized)
    if (parsed.hostname or "").lower() != "soo.sjtu.edu.cn":
        return False
    return bool(_SOO_PROFILE_RE.fullmatch(parsed.path or ""))


async def _matching_tasks(session, org_unit_name: str) -> list[CrawlTask]:
    from sqlalchemy import select

    rows = (
        await session.execute(
            select(CrawlTask)
            .where(CrawlTask.org_unit_name == org_unit_name)
            .order_by(CrawlTask.id.asc())
        )
    ).scalars().all()
    return [row for row in rows if _is_soo_profile_url(row.source_url)]


async def repair(args: argparse.Namespace) -> RepairSummary:
    db_path = Path(args.db).resolve()
    summary = RepairSummary(
        dry_run=not args.apply,
        db_path=str(db_path),
        org_unit=args.org_unit,
    )
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    if args.apply:
        backup = _backup_db(db_path)
        print(f"Created backup: {backup}")

    manager = DatabaseManager(_sqlite_url(db_path))
    try:
        async with manager.session() as session:
            org_unit = await _load_org_unit(session, args.org_unit)
            if org_unit is None:
                raise RuntimeError(f"Org unit not found: {args.org_unit}")
            tasks = await _matching_tasks(session, args.org_unit)
            summary.matching_tasks = len(tasks)
            for task in tasks:
                record = agent_detail.extract_detail_profile_record_from_snapshot(
                    task.page_text_snapshot or "",
                    page_url=task.source_url,
                )
                if not record:
                    # Snapshot did not yield a profile (URL gate or sparse capture);
                    # fall back to the URL-agnostic content extractor.
                    record = agent_detail.extract_detail_profile_record_from_snapshot(
                        task.page_text_snapshot or ""
                    )
                if record:
                    summary.snapshot_records += 1
                    if args.apply:
                        try:
                            await _replay_record(session, task, org_unit, record, summary)
                            await _mark_task_replayed(task)
                            await _ensure_detail_graph_node(
                                session,
                                task,
                                org_unit,
                                summary,
                                status=CrawlGraphNodeStatus.DONE.value,
                                last_error="sjtu_ocean_repair_replayed",
                            )
                        except Exception as exc:  # pragma: no cover - surfaced in CLI summary
                            summary.replay_errors += 1
                            print(f"Replay failed task_id={task.id} url={task.source_url}: {exc}")
                    continue

                summary.needs_refetch += 1
                if args.apply and args.reopen_empty:
                    await _reopen_task_for_refetch(task)
                    summary.reopened_tasks += 1
                    await _ensure_detail_graph_node(
                        session,
                        task,
                        org_unit,
                        summary,
                        status=CrawlGraphNodeStatus.PENDING.value,
                        last_error="sjtu_ocean_repair_refetch_required",
                    )
    finally:
        await manager.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"SQLite DB path (default: {DEFAULT_DB})")
    parser.add_argument("--org-unit", default=DEFAULT_ORG_UNIT, help=f"Org unit name (default: {DEFAULT_ORG_UNIT})")
    parser.add_argument("--apply", action="store_true", help="Write repair changes. Default is dry-run.")
    parser.add_argument(
        "--no-reopen-empty",
        dest="reopen_empty",
        action="store_false",
        help="Do not reopen profile-shaped pages whose stored snapshots do not contain a profile.",
    )
    parser.set_defaults(reopen_empty=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = asyncio.run(repair(args))
    print(json.dumps(asdict(summary), ensure_ascii=False, indent=2))
    if not args.apply:
        print("Dry-run only. Re-run with --apply to repair the DB.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
