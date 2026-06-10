from __future__ import annotations

import json
from typing import Any, Iterable

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler.db.utils import _normalize_url, _now_utc
from agents.crawler.models import (
    CrawlGraphEdge,
    CrawlGraphEdgeType,
    CrawlGraphNode,
    CrawlGraphNodeStatus,
    CrawlGraphNodeType,
)
from agents.crawler.sanitizer import normalize_org_unit_name

_GRAPH_NODE_METADATA_KEYS = {
    "source_url",
    "fetch_url",
    "identity_url",
    "fetch_action",
    "candidate_score",
    "discovery_source",
    "skip_reason",
}


def graph_node_key(
    node_type: str | CrawlGraphNodeType,
    *,
    url: str | None = None,
    org_unit_name: str | None = None,
    org_unit_id: int | None = None,
    node_key: str | None = None,
) -> str:
    if node_key:
        return str(node_key).strip()
    type_value = _enum_value(node_type)
    normalized_url = _normalize_url(url) if url else ""
    normalized_name = normalize_org_unit_name(org_unit_name or "", default="")
    if type_value == CrawlGraphNodeType.ORG_UNIT.value:
        if org_unit_id is not None:
            return f"{type_value}:id:{int(org_unit_id)}"
        if not normalized_name:
            raise ValueError("org_unit_name is required for org_unit graph node")
        return f"{type_value}:name:{normalized_name}"
    if normalized_url:
        if org_unit_id is not None:
            return f"{type_value}:org:{int(org_unit_id)}:url:{normalized_url}"
        if normalized_name:
            return f"{type_value}:org:{normalized_name}:url:{normalized_url}"
        return f"{type_value}:url:{normalized_url}"
    if normalized_name:
        return f"{type_value}:name:{normalized_name}"
    raise ValueError("url or org_unit_name is required for crawl graph node")


async def upsert_graph_node(
    session: AsyncSession,
    *,
    node_type: str | CrawlGraphNodeType,
    url: str | None = None,
    org_unit_name: str | None = None,
    org_unit_id: int | None = None,
    status: str | CrawlGraphNodeStatus = CrawlGraphNodeStatus.PENDING,
    priority_score: float = 0.0,
    confidence: float = 1.0,
    depth: int = 0,
    attempt_count: int | None = None,
    last_error: str | None = None,
    metadata: dict[str, Any] | None = None,
    metadata_json: str | None = None,
    node_key: str | None = None,
) -> CrawlGraphNode:
    type_value = _enum_value(node_type)
    status_value = _enum_value(status)
    normalized_url = _normalize_url(url) if url else ""
    normalized_name = normalize_org_unit_name(org_unit_name or "", default="")
    raw_metadata = _coerce_metadata(metadata=metadata, metadata_json=metadata_json)
    metadata_org_unit_id = raw_metadata.pop("org_unit_id", None)
    incoming_metadata = _filter_graph_node_metadata(raw_metadata)
    normalized_org_unit_id = _optional_int(
        org_unit_id if org_unit_id is not None else metadata_org_unit_id
    )
    key = graph_node_key(
        type_value,
        url=normalized_url,
        org_unit_name=normalized_name,
        org_unit_id=normalized_org_unit_id,
        node_key=node_key,
    )

    row = (
        await session.execute(select(CrawlGraphNode).where(CrawlGraphNode.node_key == key).limit(1))
    ).scalars().first()
    if row is None:
        row = CrawlGraphNode(
            node_key=key,
            type=type_value,
            url=normalized_url,
            org_unit_name=normalized_name,
            org_unit_id=normalized_org_unit_id,
            status=status_value,
            priority_score=float(priority_score or 0.0),
            base_priority=float(priority_score or 0.0),
            confidence=float(confidence or 0.0),
            depth=max(0, int(depth or 0)),
            attempt_count=max(0, int(attempt_count or 0)),
            last_error=last_error,
            metadata_json=_dump_metadata(incoming_metadata),
            created_at=_now_utc(),
            updated_at=_now_utc(),
        )
        session.add(row)
        await session.flush()
        return row

    changed = False
    if normalized_url and row.url != normalized_url:
        row.url = normalized_url
        changed = True
    if normalized_name and row.org_unit_name != normalized_name:
        row.org_unit_name = normalized_name
        changed = True
    if normalized_org_unit_id is not None and row.org_unit_id != normalized_org_unit_id:
        row.org_unit_id = normalized_org_unit_id
        changed = True
    if type_value and row.type != type_value:
        row.type = type_value
        changed = True
    if _status_rank(status_value) >= _status_rank(row.status) and row.status != status_value:
        row.status = status_value
        changed = True
    if float(priority_score or 0.0) > float(row.priority_score or 0.0):
        row.priority_score = float(priority_score or 0.0)
        changed = True
    if float(priority_score or 0.0) > float(row.base_priority or 0.0):
        row.base_priority = float(priority_score or 0.0)
        changed = True
    if float(confidence or 0.0) > float(row.confidence or 0.0):
        row.confidence = float(confidence or 0.0)
        changed = True
    if depth is not None and int(depth or 0) < int(row.depth or 0):
        row.depth = max(0, int(depth or 0))
        changed = True
    if attempt_count is not None and int(attempt_count) != int(row.attempt_count or 0):
        row.attempt_count = max(0, int(attempt_count))
        changed = True
    if last_error is not None and row.last_error != last_error:
        row.last_error = last_error
        changed = True
    if incoming_metadata:
        merged_metadata = _merge_metadata(_load_metadata(row.metadata_json), incoming_metadata)
        dumped = _dump_metadata(merged_metadata)
        if row.metadata_json != dumped:
            row.metadata_json = dumped
            changed = True
    if changed:
        row.updated_at = _now_utc()
    await session.flush()
    return row


async def upsert_graph_edge(
    session: AsyncSession,
    *,
    from_node_id: int,
    to_node_id: int,
    edge_type: str | CrawlGraphEdgeType,
    confidence: float = 1.0,
    metadata: dict[str, Any] | None = None,
    metadata_json: str | None = None,
) -> CrawlGraphEdge:
    edge_type_value = _enum_value(edge_type)
    incoming_metadata = _coerce_metadata(metadata=metadata, metadata_json=metadata_json)
    row = (
        await session.execute(
            select(CrawlGraphEdge)
            .where(
                CrawlGraphEdge.from_node_id == int(from_node_id),
                CrawlGraphEdge.to_node_id == int(to_node_id),
                CrawlGraphEdge.edge_type == edge_type_value,
            )
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        row = CrawlGraphEdge(
            from_node_id=int(from_node_id),
            to_node_id=int(to_node_id),
            edge_type=edge_type_value,
            confidence=float(confidence or 0.0),
            metadata_json=_dump_metadata(incoming_metadata),
            created_at=_now_utc(),
            updated_at=_now_utc(),
        )
        session.add(row)
        await session.flush()
        return row

    changed = False
    if float(confidence or 0.0) > float(row.confidence or 0.0):
        row.confidence = float(confidence or 0.0)
        changed = True
    if incoming_metadata:
        merged_metadata = _merge_metadata(_load_metadata(row.metadata_json), incoming_metadata)
        dumped = _dump_metadata(merged_metadata)
        if row.metadata_json != dumped:
            row.metadata_json = dumped
            changed = True
    if changed:
        row.updated_at = _now_utc()
    await session.flush()
    return row


async def mark_graph_node_status(
    session: AsyncSession,
    node_id: int | None = None,
    *,
    node_key: str | None = None,
    status: str | CrawlGraphNodeStatus,
    last_error: str | None = None,
    increment_attempt: bool = False,
) -> CrawlGraphNode | None:
    row = await _get_graph_node(session, node_id=node_id, node_key=node_key)
    if row is None:
        return None
    row.status = _enum_value(status)
    if last_error is not None:
        row.last_error = last_error
    if increment_attempt:
        row.attempt_count = int(row.attempt_count or 0) + 1
        row.priority_score = float(row.priority_score or 0.0) - 5.0
    row.updated_at = _now_utc()
    await session.flush()
    return row


async def list_ready_graph_nodes(
    session: AsyncSession,
    *,
    limit: int = 100,
    node_types: Iterable[str | CrawlGraphNodeType] | None = None,
    org_unit_names: Iterable[str] | None = None,
    org_unit_ids: Iterable[int] | None = None,
) -> list[CrawlGraphNode]:
    filters: list[Any] = [
        CrawlGraphNode.status.in_(
            [CrawlGraphNodeStatus.PENDING.value, CrawlGraphNodeStatus.RETRY.value]
        )
    ]
    if node_types:
        type_values = [_enum_value(item) for item in node_types]
        filters.append(CrawlGraphNode.type.in_(type_values))
    normalized_names = [
        normalize_org_unit_name(name, default="")
        for name in (org_unit_names or [])
        if normalize_org_unit_name(name, default="")
    ]
    normalized_ids = [
        int(org_unit_id)
        for org_unit_id in (org_unit_ids or [])
        if org_unit_id is not None
    ]
    org_filters: list[Any] = []
    if normalized_names:
        org_filters.append(CrawlGraphNode.org_unit_name.in_(normalized_names))
    if normalized_ids:
        org_filters.append(CrawlGraphNode.org_unit_id.in_(normalized_ids))
    if org_filters:
        filters.append(or_(*org_filters))

    rows = (
        await session.execute(
            select(CrawlGraphNode)
            .where(and_(*filters))
            .order_by(
                CrawlGraphNode.priority_score.desc(),
                CrawlGraphNode.confidence.desc(),
                CrawlGraphNode.depth.asc(),
                CrawlGraphNode.attempt_count.asc(),
                CrawlGraphNode.id.asc(),
            )
            .limit(max(1, int(limit or 1)))
        )
    ).scalars().all()
    return list(rows)


async def record_graph_node_result(
    session: AsyncSession,
    *,
    node_id: int | None = None,
    node_key: str | None = None,
    status: str | CrawlGraphNodeStatus,
    last_error: str | None = None,
    priority_delta: float = 0.0,
    metadata: dict[str, Any] | None = None,
) -> CrawlGraphNode | None:
    row = await _get_graph_node(session, node_id=node_id, node_key=node_key)
    if row is None:
        return None
    row.status = _enum_value(status)
    if last_error is not None:
        row.last_error = last_error
    if priority_delta:
        row.priority_score = float(row.priority_score or 0.0) + float(priority_delta)
    if metadata:
        filtered_metadata = _filter_graph_node_metadata(metadata)
        if filtered_metadata:
            row.metadata_json = _dump_metadata(_merge_metadata(_load_metadata(row.metadata_json), filtered_metadata))
    row.updated_at = _now_utc()
    await session.flush()
    return row


async def get_graph_node_by_key(session: AsyncSession, node_key: str) -> CrawlGraphNode | None:
    return (
        await session.execute(select(CrawlGraphNode).where(CrawlGraphNode.node_key == node_key).limit(1))
    ).scalars().first()


async def _get_graph_node(
    session: AsyncSession,
    *,
    node_id: int | None,
    node_key: str | None,
) -> CrawlGraphNode | None:
    if node_id is not None:
        return await session.get(CrawlGraphNode, int(node_id))
    if node_key:
        return await get_graph_node_by_key(session, node_key)
    return None


def _enum_value(value: Any) -> str:
    if isinstance(value, (CrawlGraphNodeType, CrawlGraphNodeStatus, CrawlGraphEdgeType)):
        return value.value
    return str(value or "").strip()


def _coerce_metadata(
    *,
    metadata: dict[str, Any] | None,
    metadata_json: str | None,
) -> dict[str, Any]:
    if metadata is not None:
        return dict(metadata)
    if not metadata_json:
        return {}
    return _load_metadata(metadata_json)


def _load_metadata(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dump_metadata(value: dict[str, Any]) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _merge_metadata(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = dict(current)
    for key, value in incoming.items():
        if value is None:
            continue
        if isinstance(value, list):
            existing = result.get(key)
            merged: list[Any] = []
            if isinstance(existing, list):
                merged.extend(existing)
            for item in value:
                if item not in merged:
                    merged.append(item)
            result[key] = merged
            continue
        result[key] = value
    return result


def _filter_graph_node_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result = dict(metadata or {})
    source = result.pop("source", None)
    result.pop("org_unit_id", None)
    if source is not None and "discovery_source" not in result:
        result["discovery_source"] = source
    return {key: value for key, value in result.items() if key in _GRAPH_NODE_METADATA_KEYS}


def _merge_org_unit_names(current: str | None, incoming: str) -> str:
    current_text = str(current or "").strip()
    incoming_text = incoming.strip()
    if not current_text:
        return incoming_text
    if not incoming_text or current_text == incoming_text:
        return current_text
    parts: list[str] = []
    for part in [*current_text.split(" / "), *incoming_text.split(" / ")]:
        text = part.strip()
        if text and text not in parts:
            parts.append(text)
    return " / ".join(parts)


def _status_rank(status: str) -> int:
    ranks = {
        CrawlGraphNodeStatus.PENDING.value: 0,
        CrawlGraphNodeStatus.RETRY.value: 1,
        CrawlGraphNodeStatus.IN_PROGRESS.value: 2,
        CrawlGraphNodeStatus.SKIPPED.value: 3,
        CrawlGraphNodeStatus.DONE.value: 4,
        CrawlGraphNodeStatus.FAILED.value: 4,
    }
    return ranks.get(str(status or ""), 0)


__all__ = [
    "get_graph_node_by_key",
    "graph_node_key",
    "list_ready_graph_nodes",
    "mark_graph_node_status",
    "record_graph_node_result",
    "upsert_graph_edge",
    "upsert_graph_node",
]
