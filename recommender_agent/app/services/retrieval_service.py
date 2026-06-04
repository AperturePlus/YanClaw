from typing import List, Dict, Any, Optional
from collections import defaultdict
import re

from app.core.config import get_settings
from app.services.vector_service import VectorService
from app.services.graph_service import GraphService
from app.models.item import Item
from sqlmodel import Session, col, select
from app.models.base import engine

settings = get_settings()


class RetrievalService:
    """Service for multi-channel retrieval aggregation"""

    def __init__(self):
        self.vector_service = VectorService()
        self.graph_service = GraphService()

    def retrieve_candidates(
        self,
        query: str = "",
        keywords: Optional[List[str]] = None,
        org_unit: Optional[str] = None,
        title: Optional[str] = None,
        top_k: int = 50,
    ) -> List[Dict[str, Any]]:
        """Aggregate candidates from multiple retrieval channels"""
        candidates = defaultdict(lambda: {
            "item_id": None,
            "semantic_score": 0.0,
            "graph_score": 0.0,
            "profile_score": 0.0,
            "popularity_score": 0.0,
            "sources": set(),
        })

        # Channel 1: Semantic retrieval (ChromaDB)
        vector_candidates = self.vector_service.search_similar(query, top_k=top_k)
        for vc in vector_candidates:
            item_id = vc["item_id"]
            candidates[item_id]["item_id"] = item_id
            candidates[item_id]["semantic_score"] = max(candidates[item_id]["semantic_score"], vc["score"])
            candidates[item_id]["sources"].add("vector")

        # Channel 2: Graph retrieval (NetworkX)
        if keywords or org_unit:
            graph_candidates = self.graph_service.recommend_by_graph(
                user_interests=keywords or [],
                org_unit=org_unit,
                top_k=top_k,
            )
            for gc in graph_candidates:
                item_id = gc["item_id"]
                candidates[item_id]["item_id"] = item_id
                candidates[item_id]["graph_score"] = max(candidates[item_id]["graph_score"], gc["score"])
                candidates[item_id]["sources"].add("graph")

        # Channel 3: Structured retrieval (SQL)
        with Session(engine) as session:
            sql_query = select(Item).where(Item.is_active == True)

            if org_unit:
                sql_query = sql_query.where(col(Item.org_unit).contains(org_unit))
            if title:
                sql_query = sql_query.where(col(Item.tags).contains(title))

            items = session.exec(sql_query.limit(top_k)).all()
            for item in items:
                candidates[item.id]["item_id"] = item.id
                # Base score for structured match
                candidates[item.id]["profile_score"] = 0.5
                candidates[item.id]["popularity_score"] = (item.popularity or 0.5) / 2
                candidates[item.id]["sources"].add("structured")

        # Convert to list and apply filters to every retrieval channel.
        result = []
        with Session(engine) as session:
            for item_id, data in candidates.items():
                if item_id is None:
                    continue
                item = session.exec(select(Item).where(Item.id == item_id)).first()
                if not item or not self._matches_filters(item, org_unit=org_unit, title=title):
                    continue
                result.append({
                    "item_id": item_id,
                    "semantic_score": data["semantic_score"],
                    "graph_score": data["graph_score"],
                    "profile_score": data["profile_score"],
                    "popularity_score": data["popularity_score"],
                    "sources": list(data["sources"]),
                })

        return result

    def _matches_filters(
        self,
        item: Item,
        org_unit: Optional[str] = None,
        title: Optional[str] = None,
    ) -> bool:
        if org_unit and org_unit not in (item.org_unit or ""):
            return False
        if title and not self._matches_title(item.tags, title):
            return False
        return True

    def _matches_title(self, item_title_tags: Optional[str], title_filter: str) -> bool:
        tags = item_title_tags or ""
        title = title_filter.strip()
        if not title:
            return True

        exact_titles = {"教授", "副教授", "讲师", "研究员", "副研究员", "助理研究员"}
        if title in exact_titles:
            return title in {part.strip() for part in re.split(r"[/、，,;；\s]+", tags) if part.strip()}

        return title in tags
