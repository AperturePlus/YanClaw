from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from agents.crawler import db as crawler_db
from agents.crawler.entrances import ManualOrgUnitEntrance
from agents.crawler.models import (
    CrawlGraphEdgeType,
    CrawlGraphNode,
    CrawlGraphNodeStatus,
    CrawlGraphNodeType,
    OrgUnit,
)
from agents.crawler.url_heuristics import _sanitize_url


@dataclass(frozen=True)
class GraphFetchCandidate:
    url: str
    depth: int
    label: str = ""
    org_unit_id: int | None = None
    fetch_action: dict[str, Any] | None = None
    identity_url: str | None = None
    node_id: int | None = None
    node_type: str = ""
    priority_score: float = 0.0
    confidence: float = 1.0

    @property
    def queue_url(self) -> str:
        return self.identity_url or self.url


_BASE_PRIORITY: dict[str, float] = {
    CrawlGraphNodeType.ORG_UNIT.value: 95.0,
    CrawlGraphNodeType.ORG_LISTING_URL.value: 90.0,
    CrawlGraphNodeType.FACULTY_LIST_URL.value: 80.0,
    CrawlGraphNodeType.PAGINATION_URL.value: 70.0,
    CrawlGraphNodeType.FACULTY_FOLLOWUP_URL.value: 60.0,
    CrawlGraphNodeType.DETAIL_URL.value: 40.0,
}


class GraphFrontier:
    """Persistent crawl frontier backed by the per-university SQLite database."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    async def seed_manual_entrances(
        self,
        *,
        org_units: Iterable[OrgUnit],
        manual_org_units: Iterable[ManualOrgUnitEntrance],
        org_unit_listing_urls: Iterable[str] = (),
    ) -> None:
        manual_by_name: dict[str, list[ManualOrgUnitEntrance]] = {}
        for item in manual_org_units:
            manual_by_name.setdefault(str(item.name or "").strip(), []).append(item)
        async with self.agent.db.session() as session:
            root = await crawler_db.upsert_graph_node(
                session,
                node_type=CrawlGraphNodeType.ORG_LISTING_URL,
                url=self.agent.start_url,
                status=CrawlGraphNodeStatus.DONE,
                priority_score=120.0,
                confidence=1.0,
                depth=0,
                metadata={"seed": "manifest", "university": self.agent.university_name},
            )
            for unit in org_units:
                org_node = await crawler_db.upsert_graph_node(
                    session,
                    node_type=CrawlGraphNodeType.ORG_UNIT,
                    url=getattr(unit, "url", "") or "",
                    org_unit_name=getattr(unit, "name", "") or "",
                    status=CrawlGraphNodeStatus.PENDING,
                    priority_score=self.priority_for(CrawlGraphNodeType.ORG_UNIT, source="manifest"),
                    confidence=1.0,
                    depth=1,
                    metadata={
                        "org_unit_id": int(unit.id) if getattr(unit, "id", None) is not None else None,
                        "kind": getattr(unit, "kind", "") or "",
                        "source": "manifest",
                    },
                )
                await crawler_db.upsert_graph_edge(
                    session,
                    from_node_id=int(root.id),
                    to_node_id=int(org_node.id),
                    edge_type=CrawlGraphEdgeType.SEEDED_FROM_MANIFEST,
                    confidence=1.0,
                )
                manual_items = manual_by_name.get(str(getattr(unit, "name", "") or "").strip(), [])
                for manual in manual_items:
                    faculty_url = _sanitize_url(getattr(manual, "faculty_url", ""))
                    if not faculty_url:
                        continue
                    faculty_node = await crawler_db.upsert_graph_node(
                        session,
                        node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
                        url=faculty_url,
                        org_unit_name=getattr(unit, "name", "") or "",
                        status=CrawlGraphNodeStatus.PENDING,
                        priority_score=self.priority_for(CrawlGraphNodeType.FACULTY_LIST_URL, source="manifest"),
                        confidence=1.0,
                        depth=1,
                        metadata={
                            "org_unit_id": int(unit.id) if getattr(unit, "id", None) is not None else None,
                            "source": "manifest",
                        },
                    )
                    await crawler_db.upsert_graph_edge(
                        session,
                        from_node_id=int(org_node.id),
                        to_node_id=int(faculty_node.id),
                        edge_type=CrawlGraphEdgeType.SEEDED_FROM_MANIFEST,
                        confidence=1.0,
                    )
                    await crawler_db.upsert_graph_edge(
                        session,
                        from_node_id=int(org_node.id),
                        to_node_id=int(faculty_node.id),
                        edge_type=CrawlGraphEdgeType.BELONGS_TO_ORG_UNIT,
                        confidence=1.0,
                    )
            for url in org_unit_listing_urls:
                clean_url = _sanitize_url(url)
                if not clean_url:
                    continue
                listing_node = await crawler_db.upsert_graph_node(
                    session,
                    node_type=CrawlGraphNodeType.ORG_LISTING_URL,
                    url=clean_url,
                    status=CrawlGraphNodeStatus.PENDING,
                    priority_score=self.priority_for(CrawlGraphNodeType.ORG_LISTING_URL, source="manifest"),
                    confidence=1.0,
                    depth=1,
                    metadata={"source": "manifest"},
                )
                await crawler_db.upsert_graph_edge(
                    session,
                    from_node_id=int(root.id),
                    to_node_id=int(listing_node.id),
                    edge_type=CrawlGraphEdgeType.SEEDED_FROM_MANIFEST,
                    confidence=1.0,
                )

    async def record_org_units(
        self,
        org_units: Iterable[OrgUnit],
        *,
        source_url: str = "",
        edge_type: str | CrawlGraphEdgeType = CrawlGraphEdgeType.DISCOVERED_ON_PAGE,
    ) -> None:
        async with self.agent.db.session() as session:
            source_node = None
            clean_source = _sanitize_url(source_url)
            if clean_source:
                source_node = await crawler_db.upsert_graph_node(
                    session,
                    node_type=CrawlGraphNodeType.ORG_LISTING_URL,
                    url=clean_source,
                    status=CrawlGraphNodeStatus.DONE,
                    priority_score=self.priority_for(CrawlGraphNodeType.ORG_LISTING_URL),
                    confidence=1.0,
                    depth=1,
                    metadata={"source": "org_unit_extraction"},
                )
            for unit in org_units:
                org_node = await crawler_db.upsert_graph_node(
                    session,
                    node_type=CrawlGraphNodeType.ORG_UNIT,
                    url=getattr(unit, "url", "") or "",
                    org_unit_name=getattr(unit, "name", "") or "",
                    status=CrawlGraphNodeStatus.PENDING,
                    priority_score=self.priority_for(CrawlGraphNodeType.ORG_UNIT),
                    confidence=1.0,
                    depth=1,
                    metadata={
                        "org_unit_id": int(unit.id) if getattr(unit, "id", None) is not None else None,
                        "kind": getattr(unit, "kind", "") or "",
                        "source_url": clean_source,
                    },
                )
                if source_node is not None:
                    await crawler_db.upsert_graph_edge(
                        session,
                        from_node_id=int(source_node.id),
                        to_node_id=int(org_node.id),
                        edge_type=edge_type,
                        confidence=1.0,
                    )

    async def record_discovered_links(
        self,
        *,
        source_url: str,
        links: Iterable[str | GraphFetchCandidate | Any],
        node_type: str | CrawlGraphNodeType,
        edge_type: str | CrawlGraphEdgeType,
        source_node_type: str | CrawlGraphNodeType = CrawlGraphNodeType.FACULTY_LIST_URL,
        org_unit_name: str = "",
        org_unit_id: int | None = None,
        depth: int = 1,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
        source_status: str | CrawlGraphNodeStatus = CrawlGraphNodeStatus.DONE,
    ) -> list[GraphFetchCandidate]:
        node_type_value = _enum_value(node_type)
        edge_type_value = _enum_value(edge_type)
        source_url_clean = _sanitize_url(source_url)
        if not source_url_clean:
            return []

        candidates: list[GraphFetchCandidate] = []
        async with self.agent.db.session() as session:
            source_node = await crawler_db.upsert_graph_node(
                session,
                node_type=source_node_type,
                url=source_url_clean,
                org_unit_name=org_unit_name,
                status=source_status,
                priority_score=self.priority_for(source_node_type),
                confidence=1.0,
                depth=max(0, int(depth or 0) - 1),
                metadata={"source": "discovery_parent"},
            )
            org_node = None
            if org_unit_name or org_unit_id is not None:
                org_node = await crawler_db.upsert_graph_node(
                    session,
                    node_type=CrawlGraphNodeType.ORG_UNIT,
                    org_unit_name=org_unit_name or f"id:{org_unit_id}",
                    status=CrawlGraphNodeStatus.PENDING,
                    priority_score=self.priority_for(CrawlGraphNodeType.ORG_UNIT),
                    confidence=1.0,
                    depth=1,
                    metadata={"org_unit_id": org_unit_id},
                )

            seen_keys: set[str] = set()
            for item in links:
                candidate = self._coerce_candidate(
                    item,
                    default_depth=depth,
                    default_label=org_unit_name,
                    default_org_unit_id=org_unit_id,
                )
                node_url = _sanitize_url(candidate.identity_url or candidate.url)
                fetch_url = _sanitize_url(candidate.url)
                if not node_url or not fetch_url:
                    continue
                node_metadata = dict(metadata or {})
                node_metadata.update(
                    {
                        "fetch_url": fetch_url,
                        "source_url": source_url_clean,
                        "org_unit_id": candidate.org_unit_id,
                    }
                )
                if candidate.fetch_action is not None:
                    node_metadata["fetch_action"] = candidate.fetch_action
                if candidate.identity_url:
                    node_metadata["identity_url"] = candidate.identity_url

                node = await crawler_db.upsert_graph_node(
                    session,
                    node_type=node_type_value,
                    url=node_url,
                    org_unit_name=candidate.label or org_unit_name,
                    status=CrawlGraphNodeStatus.PENDING,
                    priority_score=candidate.priority_score
                    or self.priority_for(node_type_value, source=str(node_metadata.get("source") or "")),
                    confidence=candidate.confidence or confidence,
                    depth=candidate.depth,
                    metadata=node_metadata,
                )
                key = str(node.node_key)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                await crawler_db.upsert_graph_edge(
                    session,
                    from_node_id=int(source_node.id),
                    to_node_id=int(node.id),
                    edge_type=edge_type_value,
                    confidence=confidence,
                    metadata={"source_url": source_url_clean},
                )
                candidate_org_node = org_node
                if candidate_org_node is None and (candidate.label or candidate.org_unit_id is not None):
                    candidate_org_node = await crawler_db.upsert_graph_node(
                        session,
                        node_type=CrawlGraphNodeType.ORG_UNIT,
                        org_unit_name=candidate.label or f"id:{candidate.org_unit_id}",
                        status=CrawlGraphNodeStatus.PENDING,
                        priority_score=self.priority_for(CrawlGraphNodeType.ORG_UNIT),
                        confidence=1.0,
                        depth=1,
                        metadata={"org_unit_id": candidate.org_unit_id},
                    )
                if candidate_org_node is not None:
                    await crawler_db.upsert_graph_edge(
                        session,
                        from_node_id=int(candidate_org_node.id),
                        to_node_id=int(node.id),
                        edge_type=CrawlGraphEdgeType.BELONGS_TO_ORG_UNIT,
                        confidence=confidence,
                    )
                candidates.append(self._candidate_from_node(node))

        return self.sort_candidates(candidates)

    async def next_fetch_candidates(
        self,
        *,
        limit: int = 100,
        node_types: Iterable[str | CrawlGraphNodeType] | None = None,
        org_unit_names: Iterable[str] | None = None,
        org_unit_ids: Iterable[int] | None = None,
    ) -> list[GraphFetchCandidate]:
        async with self.agent.db.session() as session:
            rows = await crawler_db.list_ready_graph_nodes(
                session,
                limit=limit,
                node_types=node_types,
                org_unit_names=org_unit_names,
                org_unit_ids=org_unit_ids,
            )
        return [self._candidate_from_node(row) for row in rows]

    async def ensure_url_node(
        self,
        *,
        url: str,
        node_type: str | CrawlGraphNodeType,
        org_unit_name: str = "",
        org_unit_id: int | None = None,
        status: str | CrawlGraphNodeStatus = CrawlGraphNodeStatus.PENDING,
        depth: int = 0,
        priority_score: float | None = None,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> GraphFetchCandidate | None:
        clean_url = _sanitize_url(url)
        if not clean_url:
            return None
        node_metadata = dict(metadata or {})
        if org_unit_id is not None:
            node_metadata["org_unit_id"] = int(org_unit_id)
        async with self.agent.db.session() as session:
            node = await crawler_db.upsert_graph_node(
                session,
                node_type=node_type,
                url=clean_url,
                org_unit_name=org_unit_name,
                status=status,
                priority_score=(
                    self.priority_for(node_type)
                    if priority_score is None
                    else float(priority_score)
                ),
                confidence=confidence,
                depth=depth,
                metadata=node_metadata,
            )
            return self._candidate_from_node(node)

    async def mark_url_status(
        self,
        *,
        url: str,
        node_type: str | CrawlGraphNodeType,
        status: str | CrawlGraphNodeStatus,
        org_unit_name: str = "",
        last_error: str | None = None,
        metadata: dict[str, Any] | None = None,
        increment_attempt: bool = False,
    ) -> None:
        clean_url = _sanitize_url(url)
        if not clean_url:
            return
        async with self.agent.db.session() as session:
            node_key = crawler_db.graph_node_key(
                node_type,
                url=clean_url,
                org_unit_name=org_unit_name,
            )
            node = await crawler_db.get_graph_node_by_key(session, node_key)
            if node is None:
                node = await crawler_db.upsert_graph_node(
                    session,
                    node_type=node_type,
                    url=clean_url,
                    org_unit_name=org_unit_name,
                    status=status,
                    priority_score=self.priority_for(node_type),
                    metadata=metadata,
                )
            else:
                await crawler_db.mark_graph_node_status(
                    session,
                    node_key=node_key,
                    status=status,
                    last_error=last_error,
                    increment_attempt=increment_attempt,
                )
                if metadata:
                    await crawler_db.record_graph_node_result(
                        session,
                        node_id=int(node.id),
                        status=status,
                        last_error=last_error,
                        metadata=metadata,
                    )

    async def mark_node_status(
        self,
        node_id: int | None,
        *,
        status: str | CrawlGraphNodeStatus,
        last_error: str | None = None,
        metadata: dict[str, Any] | None = None,
        increment_attempt: bool = False,
    ) -> None:
        if node_id is None:
            return
        async with self.agent.db.session() as session:
            await crawler_db.mark_graph_node_status(
                session,
                int(node_id),
                status=status,
                last_error=last_error,
                increment_attempt=increment_attempt,
            )
            if metadata:
                await crawler_db.record_graph_node_result(
                    session,
                    node_id=int(node_id),
                    status=status,
                    last_error=last_error,
                    metadata=metadata,
                )

    def to_queued_url(self, candidate: GraphFetchCandidate, queued_url_cls: Any) -> Any:
        kwargs = {
            "url": candidate.url,
            "depth": candidate.depth,
            "label": candidate.label,
            "org_unit_id": candidate.org_unit_id,
            "fetch_action": candidate.fetch_action,
            "identity_url": candidate.identity_url,
            "graph_node_id": candidate.node_id,
            "graph_node_type": candidate.node_type,
            "graph_priority_score": candidate.priority_score,
        }
        try:
            return queued_url_cls(**kwargs)
        except TypeError:
            kwargs.pop("graph_node_id", None)
            kwargs.pop("graph_node_type", None)
            kwargs.pop("graph_priority_score", None)
            return queued_url_cls(**kwargs)

    def sort_candidates(self, candidates: Iterable[GraphFetchCandidate]) -> list[GraphFetchCandidate]:
        return sorted(
            candidates,
            key=lambda item: (
                -float(item.priority_score or 0.0),
                -float(item.confidence or 0.0),
                int(item.depth or 0),
                item.node_id or 0,
                item.queue_url,
            ),
        )

    def priority_for(self, node_type: str | CrawlGraphNodeType, *, source: str = "") -> float:
        type_value = _enum_value(node_type)
        priority = _BASE_PRIORITY.get(type_value, 0.0)
        if source == "manifest":
            priority += 30.0
        return priority

    def _coerce_candidate(
        self,
        item: str | GraphFetchCandidate | Any,
        *,
        default_depth: int,
        default_label: str,
        default_org_unit_id: int | None,
    ) -> GraphFetchCandidate:
        if isinstance(item, GraphFetchCandidate):
            return item
        if isinstance(item, str):
            return GraphFetchCandidate(
                url=item,
                depth=default_depth,
                label=default_label,
                org_unit_id=default_org_unit_id,
            )
        return GraphFetchCandidate(
            url=str(getattr(item, "url", "") or ""),
            depth=int(getattr(item, "depth", default_depth) or default_depth),
            label=str(getattr(item, "label", "") or default_label),
            org_unit_id=getattr(item, "org_unit_id", default_org_unit_id),
            fetch_action=getattr(item, "fetch_action", None),
            identity_url=getattr(item, "identity_url", None),
            node_id=getattr(item, "graph_node_id", None),
            node_type=str(getattr(item, "graph_node_type", "") or ""),
            priority_score=float(getattr(item, "graph_priority_score", 0.0) or 0.0),
            confidence=1.0,
        )

    def _candidate_from_node(self, node: CrawlGraphNode) -> GraphFetchCandidate:
        metadata = _load_metadata(node.metadata_json)
        fetch_url = str(metadata.get("fetch_url") or node.url or "").strip()
        identity_url = str(metadata.get("identity_url") or "").strip() or None
        if not identity_url and fetch_url and node.url and fetch_url != node.url:
            identity_url = node.url
        return GraphFetchCandidate(
            url=fetch_url or node.url,
            depth=int(node.depth or 0),
            label=node.org_unit_name or "",
            org_unit_id=_optional_int(metadata.get("org_unit_id")),
            fetch_action=metadata.get("fetch_action") if isinstance(metadata.get("fetch_action"), dict) else None,
            identity_url=identity_url,
            node_id=int(node.id),
            node_type=str(node.type or ""),
            priority_score=float(node.priority_score or 0.0),
            confidence=float(node.confidence or 0.0),
        )


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


def _enum_value(value: Any) -> str:
    if isinstance(value, (CrawlGraphNodeType, CrawlGraphNodeStatus, CrawlGraphEdgeType)):
        return value.value
    return str(value or "").strip()


__all__ = ["GraphFetchCandidate", "GraphFrontier"]
