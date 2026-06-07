from __future__ import annotations

import networkx as nx
from typing import List, Optional, Dict, Any
from collections import defaultdict

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.graph_edge import GraphEdge
from app.models.org_unit import OrgUnit
from app.models.tag import Tag
from sqlmodel import Session, select
from app.models.base import engine

settings = get_settings()
logger = get_logger()


RELATION_PENALTIES = {
    "has_research_area": 1.0,
    "has_tag": 0.85,
    "similar_research": 0.65,
    "belongs_to": 0.15,
    "from_source": 0.0,
}

DIFFUSION_RELATIONS = {"has_research_area", "has_tag", "similar_research"}
ORG_EXPLANATION_RELATIONS = {"belongs_to"}
NO_DIFFUSION_RELATIONS = {"from_source"}


class GraphService:
    """Service for NetworkX knowledge graph operations"""

    def __init__(self):
        self._graph: Optional[nx.Graph[str]] = None

    def load_graph(self):
        """Load graph from graph_edges table into memory"""
        self._graph = nx.Graph()

        with Session(engine) as session:
            edges = session.exec(select(GraphEdge)).all()
            for edge in edges:
                self._graph.add_node(
                    edge.source_id,
                    type=edge.source_type,
                    label=self._label_from_node_id(edge.source_id),
                )
                self._graph.add_node(
                    edge.target_id,
                    type=edge.target_type,
                    label=self._label_from_node_id(edge.target_id),
                )
                self._graph.add_edge(
                    edge.source_id, edge.target_id,
                    relation=edge.relation,
                    weight=edge.weight,
                    confidence=edge.confidence,
                    evidence=edge.evidence,
                )
            self._hydrate_node_labels(session)

    @property
    def graph(self) -> nx.Graph[str]:
        if self._graph is None:
            self.load_graph()
        if self._graph is None:
            raise RuntimeError("Graph failed to load")
        return self._graph

    def get_neighbors(
        self, node_id: str, relation: Optional[str] = None, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Get neighbors of a node"""
        if not settings.ENABLE_GRAPH:
            return []

        try:
            neighbors = []
            for nbr in self.graph.neighbors(node_id):
                edge_data = self.graph.get_edge_data(node_id, nbr)
                if relation and edge_data.get("relation") != relation:
                    continue
                neighbors.append({
                    "node_id": nbr,
                    "relation": edge_data.get("relation", ""),
                    "weight": edge_data.get("weight", 1.0),
                })
            neighbors.sort(key=lambda x: x["weight"], reverse=True)
            return neighbors[:limit]
        except nx.NetworkXError as e:
            logger.warning("Graph neighbors error: %s", e)
            return []

    def find_paths(
        self, source: str, target: str, max_depth: int = 3
    ) -> List[Dict[str, Any]]:
        """Find paths between two nodes"""
        try:
            paths = []
            for path in nx.all_simple_paths(
                self.graph, source, target, cutoff=max_depth
            ):
                relations = []
                for i in range(len(path) - 1):
                    data = self.graph.get_edge_data(path[i], path[i + 1])
                    relations.append(data.get("relation", ""))
                paths.append({
                    "path": path,
                    "relations": relations,
                    "score": 1.0 / len(path),  # shorter = higher score
                })
            paths.sort(key=lambda x: x["score"], reverse=True)
            return paths[:5]
        except nx.NetworkXNoPath:
            return []

    def recommend_by_graph(
        self, user_interests: List[str], org_unit: Optional[str] = None, top_k: int = 10
    ) -> List[Dict[str, Any]]:
        """Graph-based recommendation using weighted bounded diffusion."""
        if not settings.ENABLE_GRAPH or not settings.GRAPH_DIFFUSION_ENABLED:
            return []

        scores: Dict[str, float] = defaultdict(float)
        evidence_by_item: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        contributing_terms: Dict[str, set[str]] = defaultdict(set)

        max_depth = max(1, int(settings.GRAPH_MAX_DEPTH))
        depth_decay = max(0.0, float(settings.GRAPH_DEPTH_DECAY))
        max_candidates = max(1, int(settings.GRAPH_MAX_CANDIDATES))

        for interest in sorted({term.strip() for term in user_interests if term.strip()}):
            for start_node in self._matching_interest_nodes(interest):
                self._diffuse_from_interest(
                    start_node=start_node,
                    term=interest,
                    max_depth=max_depth,
                    depth_decay=depth_decay,
                    scores=scores,
                    evidence_by_item=evidence_by_item,
                    contributing_terms=contributing_terms,
                )

        if org_unit:
            self._add_org_unit_explanations(
                org_unit=org_unit,
                scores=scores,
                evidence_by_item=evidence_by_item,
            )

        if not scores:
            return []

        sorted_scores = sorted(
            scores.items(),
            key=lambda item: (-item[1], self._item_sort_key(item[0])),
        )[:max_candidates]
        max_score = max(score for _, score in sorted_scores) or 1.0

        candidates = []
        for node_id, score in sorted_scores[:top_k]:
            item_id_str = node_id.replace("Item_", "")
            try:
                item_id = int(item_id_str)
                normalized_score = max(0.0, min(score / max_score, 1.0))
                graph_evidence = sorted(
                    evidence_by_item.get(node_id, []),
                    key=lambda item: (-item["raw_score"], item["path"]),
                )
                for evidence in graph_evidence:
                    evidence["normalized_score"] = max(
                        0.0,
                        min(evidence["raw_score"] / max_score, 1.0),
                    )
                candidates.append({
                    "item_id": item_id,
                    "score": normalized_score,
                    "raw_score": score,
                    "normalized_score": normalized_score,
                    "graph_evidence": graph_evidence,
                    "path": graph_evidence[0]["path"] if graph_evidence else [],
                    "relations": graph_evidence[0]["relations"] if graph_evidence else [],
                    "contributing_terms": sorted(contributing_terms.get(node_id, set())),
                    "source": "graph",
                })
            except ValueError:
                continue

        return candidates

    def _matching_interest_nodes(self, interest: str) -> List[str]:
        """Return ResearchArea/Tag nodes whose label or id matches an interest."""
        needle = interest.casefold()
        matches = []
        for node_id, data in self.graph.nodes(data=True):
            if data.get("type") not in {"ResearchArea", "Tag"}:
                continue
            label = str(data.get("label") or self._label_from_node_id(node_id))
            if needle in label.casefold() or needle in str(node_id).casefold():
                matches.append(node_id)
        return sorted(matches)

    def _diffuse_from_interest(
        self,
        *,
        start_node: str,
        term: str,
        max_depth: int,
        depth_decay: float,
        scores: Dict[str, float],
        evidence_by_item: Dict[str, List[Dict[str, Any]]],
        contributing_terms: Dict[str, set[str]],
    ) -> None:
        frontier = [(start_node, 1.0, [start_node], [])]
        best_seen: Dict[tuple[str, int], float] = {(start_node, 0): 1.0}

        for depth in range(1, max_depth + 1):
            next_frontier = []
            for node_id, path_score, path, relations in frontier:
                for neighbor in sorted(self.graph.neighbors(node_id)):
                    if neighbor in path:
                        continue
                    edge_data = self.graph.get_edge_data(node_id, neighbor) or {}
                    relation = edge_data.get("relation", "")
                    if relation in NO_DIFFUSION_RELATIONS:
                        continue
                    if relation in ORG_EXPLANATION_RELATIONS:
                        continue
                    if relation not in DIFFUSION_RELATIONS:
                        continue

                    edge_score = self._edge_diffusion_score(edge_data, depth, depth_decay)
                    if edge_score <= 0:
                        continue

                    next_score = path_score * edge_score
                    state_key = (neighbor, depth)
                    if next_score <= best_seen.get(state_key, 0.0):
                        continue
                    best_seen[state_key] = next_score

                    next_path = [*path, neighbor]
                    next_relations = [*relations, relation]
                    if self.graph.nodes[neighbor].get("type") == "Item":
                        scores[neighbor] += next_score
                        contributing_terms[neighbor].add(term)
                        evidence_by_item[neighbor].append(
                            self._build_evidence(
                                path=next_path,
                                relations=next_relations,
                                raw_score=next_score,
                                terms=[term],
                            )
                        )
                        if depth < max_depth:
                            next_frontier.append(
                                (neighbor, next_score, next_path, next_relations)
                            )
                        continue

                    if self._can_traverse_node(neighbor):
                        next_frontier.append((neighbor, next_score, next_path, next_relations))
            frontier = next_frontier

    def _add_org_unit_explanations(
        self,
        *,
        org_unit: str,
        scores: Dict[str, float],
        evidence_by_item: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        needle = org_unit.casefold()
        for node_id in sorted(self.graph.nodes()):
            data = self.graph.nodes[node_id]
            if data.get("type") != "OrgUnit":
                continue
            label = str(data.get("label") or self._label_from_node_id(node_id))
            if needle not in label.casefold() and needle not in str(node_id).casefold():
                continue
            for neighbor in sorted(self.graph.neighbors(node_id)):
                if self.graph.nodes[neighbor].get("type") != "Item":
                    continue
                edge_data = self.graph.get_edge_data(node_id, neighbor) or {}
                if edge_data.get("relation") != "belongs_to":
                    continue
                raw_score = self._edge_diffusion_score(edge_data, 1, 1.0)
                if raw_score <= 0:
                    continue
                scores[neighbor] += raw_score
                evidence_by_item[neighbor].append(
                    self._build_evidence(
                        path=[node_id, neighbor],
                        relations=["belongs_to"],
                        raw_score=raw_score,
                        terms=[],
                    )
                )

    def _hydrate_node_labels(self, session: Session) -> None:
        """Attach human-readable labels for id-based Tag and OrgUnit nodes."""
        if self._graph is None:
            return

        tags = {f"Tag_{tag.id}": tag.name for tag in session.exec(select(Tag)).all()}
        org_units = {
            f"OrgUnit_{org_unit.id}": org_unit.name
            for org_unit in session.exec(select(OrgUnit)).all()
        }
        for node_id, label in {**tags, **org_units}.items():
            if self._graph.has_node(node_id):
                self._graph.nodes[node_id]["label"] = label

    def _edge_diffusion_score(
        self, edge_data: Dict[str, Any], depth: int, depth_decay: float
    ) -> float:
        relation = edge_data.get("relation", "")
        relation_penalty = RELATION_PENALTIES.get(relation, 0.0)
        if relation_penalty <= 0:
            return 0.0
        return (
            float(edge_data.get("weight", 1.0) or 0.0)
            * float(edge_data.get("confidence", 1.0) or 0.0)
            * relation_penalty
            * (depth_decay ** max(depth - 1, 0))
        )

    def _build_evidence(
        self,
        *,
        path: List[str],
        relations: List[str],
        raw_score: float,
        terms: List[str],
    ) -> Dict[str, Any]:
        edge_evidence = []
        for index, relation in enumerate(relations):
            data = self.graph.get_edge_data(path[index], path[index + 1]) or {}
            edge_evidence.append({
                "relation": relation,
                "weight": data.get("weight", 1.0),
                "confidence": data.get("confidence", 1.0),
                "evidence": data.get("evidence"),
            })
        return {
            "path": path,
            "relations": relations,
            "raw_score": raw_score,
            "normalized_score": 0.0,
            "terms": sorted(terms),
            "edge_evidence": edge_evidence,
        }

    def _can_traverse_node(self, node_id: str) -> bool:
        node_type = self.graph.nodes[node_id].get("type")
        return node_type in {"ResearchArea"}

    def _label_from_node_id(self, node_id: str) -> str:
        return str(node_id).split("_", maxsplit=1)[-1]

    def _item_sort_key(self, node_id: str) -> int:
        try:
            return int(node_id.replace("Item_", ""))
        except ValueError:
            return 0
