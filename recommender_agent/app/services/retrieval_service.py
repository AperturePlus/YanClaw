from collections import defaultdict
from typing import Any
import re

from app.core.config import get_settings
from app.services.domain_gate import DomainGateResult, DomainGateService
from app.services.vector_service import VectorService
from app.services.graph_service import GraphService
from app.models.item import Item
from sqlmodel import Session, col, select
from app.models.base import engine

settings = get_settings()

CandidateData = dict[str, Any]


class RetrievalService:
    """Service for multi-channel retrieval aggregation"""

    def __init__(self):
        self.vector_service = VectorService()
        self.graph_service = GraphService()
        self.domain_gate_service = DomainGateService(
            ontology_path=settings.DOMAIN_ONTOLOGY_PATH,
            min_confidence=settings.DOMAIN_GATE_MIN_CONFIDENCE,
            mode=settings.DOMAIN_GATE_MODE,
            fallback_on_empty=settings.DOMAIN_GATE_FALLBACK_ON_EMPTY,
        )

    def retrieve_candidates(
        self,
        query: str = "",
        keywords: list[str] | None = None,
        org_unit: str | None = None,
        title: str | None = None,
        top_k: int = 50,
    ) -> list[dict[str, Any]]:
        """Aggregate candidates from multiple retrieval channels"""
        retrieval_k = self._expanded_top_k(top_k)
        intent_keywords = keywords or []
        domain_inference = self.domain_gate_service.infer_domain(query, intent_keywords)
        candidates: defaultdict[int, CandidateData] = defaultdict(self._new_candidate_data)

        # Channel 1: Semantic retrieval (ChromaDB)
        vector_candidates = self.vector_service.search_similar(query, top_k=retrieval_k)
        for vc in vector_candidates:
            item_id = vc["item_id"]
            candidates[item_id]["item_id"] = item_id
            candidates[item_id]["semantic_score"] = max(
                float(candidates[item_id]["semantic_score"]),
                float(vc["score"]),
            )
            candidates[item_id]["sources"].add("vector")

        # Channel 2: Graph retrieval (NetworkX)
        if keywords or org_unit:
            graph_candidates = self.graph_service.recommend_by_graph(
                user_interests=intent_keywords,
                org_unit=org_unit,
                top_k=retrieval_k,
            )
            for gc in graph_candidates:
                item_id = gc["item_id"]
                candidates[item_id]["item_id"] = item_id
                candidates[item_id]["graph_score"] = max(
                    float(candidates[item_id]["graph_score"]),
                    float(gc["score"]),
                )
                candidates[item_id]["graph_evidence"] = self._merge_graph_evidence(
                    candidates[item_id]["graph_evidence"],
                    gc.get("graph_evidence", []),
                )
                if gc.get("path") and not candidates[item_id]["path"]:
                    candidates[item_id]["path"] = gc["path"]
                if gc.get("relations") and not candidates[item_id]["relations"]:
                    candidates[item_id]["relations"] = gc["relations"]
                candidates[item_id]["contributing_terms"] = sorted(
                    {
                        *candidates[item_id]["contributing_terms"],
                        *gc.get("contributing_terms", []),
                    }
                )
                candidates[item_id]["sources"].add("graph")

        # Channel 3: Structured retrieval (SQL)
        with Session(engine) as session:
            sql_query = select(Item).where(Item.is_active == True)

            if org_unit:
                sql_query = sql_query.where(col(Item.org_unit).contains(org_unit))
            if title:
                sql_query = sql_query.where(col(Item.tags).contains(title))

            items = session.exec(sql_query.limit(retrieval_k)).all()
            for item in items:
                if item.id is None:
                    continue
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
                    "graph_evidence": data["graph_evidence"],
                    "path": data["path"],
                    "relations": data["relations"],
                    "contributing_terms": data["contributing_terms"],
                    "sources": list(data["sources"]),
                })

            if settings.ENABLE_DOMAIN_GATE:
                gate_result = self.domain_gate_service.score_candidates(
                    result,
                    domain_inference,
                    session=session,
                )
                result = self._annotate_domain_gate_result(gate_result.candidates, gate_result)

        return result

    def _new_candidate_data(self) -> CandidateData:
        return {
            "item_id": None,
            "semantic_score": 0.0,
            "graph_score": 0.0,
            "profile_score": 0.0,
            "popularity_score": 0.0,
            "graph_evidence": [],
            "path": [],
            "relations": [],
            "contributing_terms": [],
            "sources": set(),
        }

    def _expanded_top_k(self, top_k: int) -> int:
        if not settings.ENABLE_RERANKER:
            return top_k
        return max(top_k, settings.RERANK_TOP_N or 50)

    def _merge_graph_evidence(
        self,
        current: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged = [*current]
        seen = {
            (
                tuple(evidence.get("path", [])),
                tuple(evidence.get("relations", [])),
                tuple(evidence.get("terms", [])),
            )
            for evidence in merged
        }
        for evidence in incoming:
            key = (
                tuple(evidence.get("path", [])),
                tuple(evidence.get("relations", [])),
                tuple(evidence.get("terms", [])),
            )
            if key in seen:
                continue
            seen.add(key)
            merged.append(evidence)
        return merged

    def _annotate_domain_gate_result(
        self,
        candidates: list[dict[str, Any]],
        gate_result: DomainGateResult,
    ) -> list[dict[str, Any]]:
        annotated = []
        for candidate in candidates:
            enriched = dict(candidate)
            enriched["domain"] = gate_result.domain
            enriched["domain_gate_confidence"] = gate_result.domain_confidence
            if gate_result.fallback_reason:
                evidence = list(enriched.get("domain_evidence") or [])
                if gate_result.fallback_reason not in evidence:
                    evidence.append(gate_result.fallback_reason)
                enriched["domain_evidence"] = evidence
                enriched["domain_fallback_reason"] = gate_result.fallback_reason
            annotated.append(enriched)
        return annotated

    def _matches_filters(
        self,
        item: Item,
        org_unit: str | None = None,
        title: str | None = None,
    ) -> bool:
        if org_unit and org_unit not in (item.org_unit or ""):
            return False
        if title and not self._matches_title(item.tags, title):
            return False
        return True

    def _matches_title(self, item_title_tags: str | None, title_filter: str) -> bool:
        tags = item_title_tags or ""
        title = title_filter.strip()
        if not title:
            return True

        exact_titles = {"教授", "副教授", "讲师", "研究员", "副研究员", "助理研究员"}
        if title in exact_titles:
            return title in {part.strip() for part in re.split(r"[/、，,;；\s]+", tags) if part.strip()}

        return title in tags
