from typing import List, Optional, Dict, Any
import time
import uuid
import asyncio
import json

from app.core.config import get_settings
from app.core.response import success_response, error_response
from app.services.retrieval_service import RetrievalService
from app.services.ranking_service import RankingService
from app.services.llm_service import LLMService
from app.services.graph_service import GraphService
from app.models.item import Item
from app.models.recommendation_log import RecommendationLog
from sqlmodel import Session, select
from app.models.base import engine

settings = get_settings()


class RecommendationService:
    """Main recommendation service orchestrating the full pipeline"""

    def __init__(self):
        self.retrieval_service = RetrievalService()
        self.ranking_service = RankingService()
        self.llm_service = LLMService()
        self.graph_service = GraphService()

    async def recommend(
        self,
        query: str = "",
        user_id: Optional[int] = None,
        top_k: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Full recommendation pipeline"""
        request_id = f"req_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        start_time = time.time()

        top_k = top_k or settings.DEFAULT_TOP_K
        options = options or {}
        filters = filters or {}

        # Step 1: Parse intent
        intent = await self.llm_service.parse_intent(query)

        # Step 2: Retrieve candidates
        candidates = self.retrieval_service.retrieve_candidates(
            query=query,
            keywords=intent.get("keywords", []),
            org_unit=filters.get("org_unit") or intent.get("org_unit"),
            title=filters.get("title"),
            top_k=top_k * 3,
        )

        # Step 3: Rank candidates
        ranked = self.ranking_service.rank_candidates(candidates, top_k=top_k)

        # Step 4: Fetch full item details, then generate explanations concurrently.
        recommendation_payloads = []
        with Session(engine) as session:
            for rc in ranked:
                item = session.exec(select(Item).where(Item.id == rc["item_id"])).first()
                if not item:
                    continue
                recommendation_payloads.append({
                    "item_id": item.id,
                    "title": item.title,
                    "category": item.category,
                    "org_unit": item.org_unit,
                    "tags": item.tags,
                    "research_areas": item.research_areas,
                    "description": item.description,
                    "score": rc["final_score"],
                    "evidence": {
                        "semantic_score": rc.get("semantic_score", 0),
                        "graph_score": rc.get("graph_score", 0),
                        "profile_score": rc.get("profile_score", 0),
                        "popularity_score": rc.get("popularity_score", 0),
                    },
                    "sources": rc.get("sources", []),
                    "graph_paths": [],
                })

        explanations = await asyncio.gather(
            *(
                self.llm_service.generate_explanation(
                    item_title=payload["title"],
                    query=query,
                    graph_paths=payload["graph_paths"],
                )
                for payload in recommendation_payloads
            )
        )
        recommendations = [
            {**payload, "reason": explanation}
            for payload, explanation in zip(recommendation_payloads, explanations)
        ]
        recommendations.sort(key=lambda recommendation: recommendation["score"], reverse=True)
        graph_paths = self._build_graph_paths(recommendations)

        latency_ms = int((time.time() - start_time) * 1000)

        # Step 5: Log recommendation
        log = RecommendationLog(
            request_id=request_id,
            user_id=user_id,
            query=query,
            strategy="hybrid",
            candidate_count=len(ranked),
            result_item_ids=json.dumps([r["item_id"] for r in recommendations]),
            latency_ms=latency_ms,
            debug_json=json.dumps({"intent": intent, "options": options}),
        )
        with Session(engine) as session:
            session.add(log)
            session.commit()

        return success_response(
            data={
                "request_id": request_id,
                "query_understanding": {
                    "intent": intent.get("intent", ""),
                    "keywords": intent.get("keywords", []),
                    "tags": intent.get("tags", []),
                },
                "recommendations": recommendations,
                "graph_paths": graph_paths,
                "latency_ms": latency_ms,
            },
            request_id=request_id,
        )

    def _build_graph_paths(self, recommendations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Build a lightweight graph view for the current recommendation set."""
        graph_paths = []
        seen_edges = set()

        for recommendation in recommendations:
            org_unit = recommendation.get("org_unit")
            if not org_unit:
                continue

            source = f"Item_{recommendation['item_id']}"
            target = f"Org_{org_unit}"
            edge_key = (source, target, "belongs_to")
            if edge_key in seen_edges:
                continue

            seen_edges.add(edge_key)
            graph_paths.append({
                "source": source,
                "source_label": recommendation.get("title", source),
                "source_type": "Item",
                "target": target,
                "target_label": org_unit,
                "target_type": "OrgUnit",
                "relation": "belongs_to",
                "weight": 1.0,
            })

        return graph_paths
