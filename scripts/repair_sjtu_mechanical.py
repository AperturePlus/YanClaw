"""One-time repair for SJTU Mechanical College teacher_directory profiles.

The 2026-06-12 run stored SJTU Mechanical profile URLs as traversal-only list
tasks because the shared detail matcher did not recognize
``teacher_directoryN/<slug>.html``. This script replays any extractable stored
    snapshots through the normal professor upsert path when --replay-snapshots is
    provided, then reopens the remaining profile-shaped pages as detail tasks/nodes
    so the next crawler resume refetches them under the corrected matcher.

Dry-run by default. Use --apply to write changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.crawler import agent_detail  # noqa: E402
from agents.crawler import db as crawler_db  # noqa: E402
from agents.crawler.db.graph import graph_node_key  # noqa: E402
from agents.crawler.db.utils import _normalize_url, _now_utc  # noqa: E402
from agents.crawler.models import (  # noqa: E402
    Academician,
    CrawlGraphNode,
    CrawlGraphNodeStatus,
    CrawlGraphNodeType,
    CrawlPageCache,
    CrawlTask,
    CrawlTaskKind,
    CrawlTaskStatus,
    OrgUnit,
)
from agents.crawler.sanitizer import (  # noqa: E402
    contains_postdoc_hint,
    contains_retired_hint,
    sanitize_professor_payload,
)
from runtime.database import DatabaseManager  # noqa: E402


DEFAULT_DB = ROOT / "data" / "universities" / "sjtu.edu.cn.db"
DEFAULT_ORG_UNIT = "机械与动力工程学院"
DEFAULT_UNIVERSITY = "上海交通大学"
_SJTU_ME_PROFILE_RE = re.compile(r"^/teacher_directory\d+/[a-z][a-z0-9_]*\.s?html?$", re.IGNORECASE)


@dataclass
class RepairSummary:
    dry_run: bool
    db_path: str
    org_unit: str
    matching_tasks: int = 0
    snapshot_records: int = 0
    link_signal_records: int = 0
    professors_created: int = 0
    professors_updated: int = 0
    professors_unchanged: int = 0
    academicians_saved: int = 0
    filtered_retired: int = 0
    filtered_postdoc: int = 0
    replay_errors: int = 0
    reopened_tasks: int = 0
    reopened_graph_nodes: int = 0
    created_graph_nodes: int = 0
    graph_conflicts: int = 0
    needs_refetch: int = 0


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


def _is_sjtu_me_profile_url(url: str) -> bool:
    normalized = _normalize_url(url)
    if not normalized:
        return False
    parsed = urlparse(normalized)
    if (parsed.hostname or "").lower() != "me.sjtu.edu.cn":
        return False
    if not _SJTU_ME_PROFILE_RE.fullmatch(parsed.path or ""):
        return False
    return agent_detail._looks_like_profile_detail_url(normalized)


def _backup_db(path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = path.with_suffix(path.suffix + f".bak_sjtu_me_{stamp}")
    shutil.copy2(path, backup)
    return backup


def _allowed_tools_json() -> str:
    return json.dumps(["save_professors"], ensure_ascii=False, separators=(",", ":"))


def _clean_anchor_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\u3000", " ").split())


def _record_from_link_signal(anchor_text: Any, url: str) -> dict[str, Any] | None:
    text = _clean_anchor_text(anchor_text)
    if not text:
        return None
    name = re.split(r"[/／|｜\n\r]", text, maxsplit=1)[0].strip()
    name = re.sub(r"\s+", "", name)
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,4}", name):
        return None
    if name in {"本科生", "研究生", "博士后", "科研型", "教学型", "教师名录"}:
        return None
    record: dict[str, Any] = {"name": name, "homepage": _normalize_url(url)}
    for title in (
        "特聘研究员",
        "副研究员",
        "助理研究员",
        "研究员",
        "副教授",
        "助理教授",
        "教授",
        "讲师",
        "高级工程师",
        "工程师",
    ):
        if title in text:
            record["title"] = title
            break
    research_match = re.search(r"研究方向\s*[：:]\s*(?P<value>.+)", text)
    if research_match:
        research = research_match.group("value").strip()
        research = re.split(r"(?:欢迎|本人长期|邮箱|电子邮件|电话)", research, maxsplit=1)[0].strip()
        if research:
            record["research_areas"] = research[:500]
    return record


async def _link_signal_records(session: Any) -> dict[str, dict[str, Any]]:
    rows = (
        await session.execute(
            select(CrawlPageCache)
            .where(CrawlPageCache.url.like("%teacher_directory%"))
            .order_by(CrawlPageCache.id.asc())
        )
    ).scalars().all()
    records: dict[str, dict[str, Any]] = {}
    scores: dict[str, int] = {}
    for row in rows:
        try:
            signals = json.loads(row.link_signals_json or "[]")
        except Exception:
            continue
        if not isinstance(signals, list):
            continue
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            url = _normalize_url(signal.get("url"))
            if not _is_sjtu_me_profile_url(url):
                continue
            record = _record_from_link_signal(signal.get("anchor_text"), url)
            if not record:
                continue
            score = 1
            if record.get("title"):
                score += 2
            if record.get("research_areas"):
                score += 3
            if len(_clean_anchor_text(signal.get("anchor_text"))) <= 20:
                score += 1
            if score > scores.get(url, -1):
                records[url] = record
                scores[url] = score
    return records


async def _load_org_unit(session: Any, name: str) -> OrgUnit | None:
    return (
        await session.execute(
            select(OrgUnit)
            .where(OrgUnit.name == name)
            .order_by(OrgUnit.id.asc())
            .limit(1)
        )
    ).scalars().first()


async def _matching_tasks(session: Any, org_unit_name: str) -> list[CrawlTask]:
    rows = (
        await session.execute(
            select(CrawlTask)
            .where(CrawlTask.org_unit_name == org_unit_name)
            .order_by(CrawlTask.id.asc())
        )
    ).scalars().all()
    return [row for row in rows if _is_sjtu_me_profile_url(row.source_url)]


async def _graph_nodes_for_url(session: Any, url: str, org_unit: OrgUnit) -> list[CrawlGraphNode]:
    normalized = _normalize_url(url)
    rows = (
        await session.execute(
            select(CrawlGraphNode)
            .where(
                CrawlGraphNode.url == normalized,
                (CrawlGraphNode.org_unit_name == org_unit.name)
                | (CrawlGraphNode.org_unit_id == org_unit.id),
            )
            .order_by(CrawlGraphNode.id.asc())
        )
    ).scalars().all()
    return list(rows)


async def _replay_record(
    session: Any,
    task: CrawlTask,
    org_unit: OrgUnit,
    record: dict[str, Any],
    summary: RepairSummary,
) -> None:
    if contains_retired_hint(record.get("name"), record.get("title"), record.get("bio"), task.source_url):
        summary.filtered_retired += 1
        return
    if contains_postdoc_hint(
        name=record.get("name"),
        title=record.get("title"),
        bio=record.get("bio"),
        source_url=task.source_url,
    ):
        summary.filtered_postdoc += 1
        return

    cleaned, is_academician = sanitize_professor_payload(record, org_unit_name=org_unit.name)
    data = {
        **cleaned,
        "org_unit_url": org_unit.url,
        "source_url": task.source_url,
    }
    if is_academician:
        result = await crawler_db.upsert_academician_with_status(session, data)
        if result.status in {"created", "updated", "unchanged"}:
            summary.academicians_saved += 1
        return

    matched_academician, _reason = await crawler_db.match_academician_for_professor(
        session,
        name=str(cleaned.get("name") or ""),
        org_unit_name=org_unit.name,
        email=cleaned.get("email"),
        homepage=cleaned.get("homepage"),
        external_link=cleaned.get("external_link"),
    )
    if isinstance(matched_academician, Academician):
        await crawler_db.merge_into_academician_from_professor(
            session,
            matched_academician,
            title=cleaned.get("title"),
            research_areas=cleaned.get("research_areas"),
            email=cleaned.get("email"),
            phone=cleaned.get("phone"),
            homepage=cleaned.get("homepage"),
            external_link=cleaned.get("external_link"),
            bio=cleaned.get("bio"),
            enrollment_pref=cleaned.get("enrollment_pref"),
            publications=cleaned.get("publications"),
        )
        summary.academicians_saved += 1
        return

    result = await crawler_db.upsert_professor_with_status(session, data)
    if result.status == "created":
        summary.professors_created += 1
    elif result.status == "updated":
        summary.professors_updated += 1
    else:
        summary.professors_unchanged += 1


async def _mark_task_replayed(task: CrawlTask) -> None:
    task.task_kind = CrawlTaskKind.DETAIL_PAGE.value
    task.status = CrawlTaskStatus.DONE.value
    task.allowed_tools = _allowed_tools_json()
    task.last_error = "sjtu_mechanical_repair_replayed"
    task.updated_at = _now_utc()


async def _reopen_task_for_refetch(task: CrawlTask) -> None:
    task.task_kind = CrawlTaskKind.DETAIL_PAGE.value
    task.status = CrawlTaskStatus.PENDING.value
    task.allowed_tools = _allowed_tools_json()
    task.attempt = 0
    task.priority = min(int(task.priority or 0), -75)
    task.last_error = "sjtu_mechanical_repair_refetch_required"
    task.updated_at = _now_utc()


async def _retarget_graph_node_to_detail(
    session: Any,
    node: CrawlGraphNode,
    org_unit: OrgUnit,
    summary: RepairSummary,
    *,
    status: str,
    last_error: str,
) -> None:
    target_key = graph_node_key(
        CrawlGraphNodeType.DETAIL_URL,
        url=node.url,
        org_unit_name=node.org_unit_name or org_unit.name,
        org_unit_id=node.org_unit_id,
    )
    conflict = (
        await session.execute(
            select(CrawlGraphNode)
            .where(CrawlGraphNode.node_key == target_key, CrawlGraphNode.id != node.id)
            .limit(1)
        )
    ).scalars().first()
    if conflict is not None:
        summary.graph_conflicts += 1
        conflict.type = CrawlGraphNodeType.DETAIL_URL.value
        conflict.status = status
        conflict.last_error = last_error
        conflict.attempt_count = 0
        conflict.priority_score = max(float(conflict.priority_score or 0.0), 75.0)
        conflict.base_priority = max(float(conflict.base_priority or 0.0), 75.0)
        conflict.updated_at = _now_utc()
        node.status = CrawlGraphNodeStatus.SKIPPED.value
        node.last_error = f"sjtu_mechanical_repair_superseded_by:{int(conflict.id)}"
        node.updated_at = _now_utc()
        return

    node.node_key = target_key
    node.type = CrawlGraphNodeType.DETAIL_URL.value
    node.status = status
    node.last_error = last_error
    node.attempt_count = 0
    node.priority_score = max(float(node.priority_score or 0.0), 75.0)
    node.base_priority = max(float(node.base_priority or 0.0), 75.0)
    node.updated_at = _now_utc()
    summary.reopened_graph_nodes += 1


async def _ensure_detail_graph_node(
    session: Any,
    task: CrawlTask,
    org_unit: OrgUnit,
    summary: RepairSummary,
    *,
    status: str,
    last_error: str,
) -> None:
    nodes = await _graph_nodes_for_url(session, task.source_url, org_unit)
    if nodes:
        await _retarget_graph_node_to_detail(
            session,
            nodes[0],
            org_unit,
            summary,
            status=status,
            last_error=last_error,
        )
        for duplicate in nodes[1:]:
            duplicate.status = CrawlGraphNodeStatus.SKIPPED.value
            duplicate.last_error = f"sjtu_mechanical_repair_duplicate_of:{int(nodes[0].id)}"
            duplicate.updated_at = _now_utc()
        return

    node = await crawler_db.upsert_graph_node(
        session,
        node_type=CrawlGraphNodeType.DETAIL_URL,
        url=task.source_url,
        org_unit_name=org_unit.name,
        org_unit_id=org_unit.id,
        status=status,
        priority_score=75.0,
        confidence=1.0,
        depth=3,
        last_error=last_error,
        metadata={"source": "sjtu_mechanical_repair"},
    )
    node.attempt_count = 0
    node.updated_at = _now_utc()
    summary.created_graph_nodes += 1


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
            link_records = await _link_signal_records(session) if args.use_link_signals else {}
            summary.matching_tasks = len(tasks)
            for task in tasks:
                record = agent_detail.extract_detail_profile_record_from_snapshot(
                    task.page_text_snapshot or "",
                    page_url=task.source_url,
                )
                if record:
                    summary.snapshot_records += 1
                    if args.replay_snapshots:
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
                                    last_error="sjtu_mechanical_repair_replayed",
                                )
                            except Exception as exc:  # pragma: no cover - surfaced in CLI summary
                                summary.replay_errors += 1
                                print(f"Replay failed task_id={task.id} url={task.source_url}: {exc}")
                        continue

                link_record = link_records.get(_normalize_url(task.source_url))
                if link_record:
                    summary.link_signal_records += 1
                    if args.apply:
                        try:
                            await _replay_record(session, task, org_unit, link_record, summary)
                        except Exception as exc:  # pragma: no cover - surfaced in CLI summary
                            summary.replay_errors += 1
                            print(f"Link-signal replay failed task_id={task.id} url={task.source_url}: {exc}")

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
                        last_error="sjtu_mechanical_repair_refetch_required",
                    )
    finally:
        await manager.close()
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help=f"SQLite DB path (default: {DEFAULT_DB})")
    parser.add_argument("--org-unit", default=DEFAULT_ORG_UNIT, help=f"Org unit name (default: {DEFAULT_ORG_UNIT})")
    parser.add_argument("--university", default=DEFAULT_UNIVERSITY, help=argparse.SUPPRESS)
    parser.add_argument("--apply", action="store_true", help="Write repair changes. Default is dry-run.")
    parser.add_argument(
        "--replay-snapshots",
        action="store_true",
        help="Save records synthesized from stored snapshots. Default reopens pages for refetch.",
    )
    parser.add_argument(
        "--no-link-signals",
        dest="use_link_signals",
        action="store_false",
        help="Do not seed professor rows from cached roster link-signal anchors.",
    )
    parser.add_argument(
        "--no-reopen-empty",
        dest="reopen_empty",
        action="store_false",
        help="Do not reopen profile-shaped pages whose stored snapshots do not contain a profile.",
    )
    parser.set_defaults(reopen_empty=True, use_link_signals=True)
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
