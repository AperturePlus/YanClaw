import networkx as nx
from typing import List, Optional, Dict, Any, Tuple
from collections import defaultdict

from app.core.config import get_settings
from app.models.graph_edge import GraphEdge
from sqlmodel import Session, select
from app.models.base import engine

settings = get_settings()


class GraphService:
    """Service for NetworkX knowledge graph operations"""

    def __init__(self):
        self._graph: Optional[nx.Graph] = None

    def load_graph(self):
        """Load graph from graph_edges table into memory"""
        self._graph = nx.Graph()

        with Session(engine) as session:
            edges = session.exec(select(GraphEdge)).all()
            for edge in edges:
                self._graph.add_node(edge.source_id, type=edge.source_type)
                self._graph.add_node(edge.target_id, type=edge.target_type)
                self._graph.add_edge(
                    edge.source_id, edge.target_id,
                    relation=edge.relation, weight=edge.weight
                )

    @property
    def graph(self) -> nx.Graph:
        if self._graph is None:
            self.load_graph()
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
        except Exception as e:
            print(f"Graph neighbors error: {e}")
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
        """Graph-based recommendation using user interests"""
        if not settings.ENABLE_GRAPH:
            return []

        scores = defaultdict(float)

        for interest in user_interests:
            # Find items matching interests via tags
            for node in self.graph.nodes():
                if self.graph.nodes[node].get("type") == "Tag":
                    if interest.lower() in self.graph.nodes[node].get("label", "").lower():
                        # Find connected items
                        for nbr in self.graph.neighbors(node):
                            if self.graph.nodes[nbr].get("type") == "Item":
                                scores[nbr] += 1.0

        # Add org_unit boost
        if org_unit:
            for node in self.graph.nodes():
                if self.graph.nodes[node].get("type") == "Item":
                    org = self.graph.nodes[node].get("org_unit", "")
                    if org == org_unit:
                        scores[node] += 0.5

        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        candidates = []
        for node_id, score in sorted_scores[:top_k]:
            item_id_str = node_id.replace("Item_", "")
            try:
                item_id = int(item_id_str)
                candidates.append({
                    "item_id": item_id,
                    "score": min(score, 1.0),
                    "source": "graph",
                })
            except ValueError:
                continue

        return candidates
