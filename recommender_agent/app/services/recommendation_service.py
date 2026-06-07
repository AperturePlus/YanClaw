from typing import List, Optional, Dict, Any
import asyncio
import time
import uuid
import json
import re

from app.core.config import get_settings
from app.core.response import error_response, success_response
from app.services.retrieval_service import RetrievalService
from app.services.ranking_service import RankingService
from app.services.reranker_service import RerankerService
from app.services.llm_service import LLMService
from app.services.graph_service import GraphService
from app.models.item import Item
from app.models.org_unit import OrgUnit
from app.models.recommendation_log import RecommendationLog
from sqlmodel import Session, select
from app.models.base import engine

settings = get_settings()


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _split_text_list(value: str | None) -> List[str]:
    text = _clean_text(value)
    if text is None:
        return []
    return [part.strip() for part in re.split(r"[/、，,;；\n]+", text) if part.strip()]


def _dedupe(values: List[str]) -> List[str]:
    seen = set()
    result: List[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _find_overlaps(candidates: List[str], targets: List[str]) -> List[str]:
    matches: List[str] = []
    for candidate in candidates:
        candidate_lower = candidate.lower()
        for target in targets:
            target_text = str(target).strip()
            if not target_text:
                continue
            target_lower = target_text.lower()
            if target_lower in candidate_lower or candidate_lower in target_lower:
                matches.append(candidate)
                break
    return _dedupe(matches)


def _generate_evidence_reasons(
    item: Item,
    intent: Dict[str, Any],
    ranking_evidence: Dict[str, Any],
    retrieval_sources: List[str] | None = None,
) -> List[str]:
    """Generate Chinese recommendation reasons from concrete item and ranking evidence."""
    query_understanding = intent.get("query_understanding", {})
    query = _clean_text(intent.get("query")) or "当前查询"
    reasons: List[str] = []

    research_fields = _split_text_list(item.research_areas)
    research_interests = query_understanding.get("research_interests") or []
    matched_areas = _find_overlaps(research_fields, research_interests)
    if matched_areas:
        reasons.append(
            f"研究方向包含{'、'.join(matched_areas)}，与用户查询的\"{query}\"高度相关"
        )

    item_org_unit = _clean_text(item.org_unit)
    intent_org_unit = _clean_text(intent.get("org_unit"))
    if item_org_unit and intent_org_unit and (
        intent_org_unit in item_org_unit or item_org_unit in intent_org_unit
    ):
        reasons.append(f"所在学院为{item_org_unit}，符合查询中的学院偏好")

    item_title = _clean_text(item.tags)
    intent_title = _clean_text(intent.get("title"))
    if item_title and intent_title and (
        intent_title in item_title or item_title in intent_title
    ):
        reasons.append(f"职称{item_title}，匹配用户的导师类型偏好")

    semantic_score = float(ranking_evidence.get("semantic_score") or 0)
    graph_score = float(ranking_evidence.get("graph_score") or 0)
    domain_score = float(ranking_evidence.get("domain_score") or 0)
    rerank_score = float(ranking_evidence.get("rerank_score") or 0)
    if semantic_score > 0 or graph_score > 0:
        reasons.append(
            f"通过语义检索（semantic_score: {semantic_score:.4f}）和图关系（graph_score: {graph_score:.4f}）综合匹配"
        )
    elif retrieval_sources:
        reasons.append(f"召回来源为{'、'.join(retrieval_sources)}，来自实际检索结果")

    domain_evidence = ranking_evidence.get("domain_evidence") or []
    if domain_score > 0 or domain_evidence:
        reasons.append(
            f"领域匹配得分为{domain_score:.4f}，依据为{'、'.join(str(item) for item in domain_evidence) or '领域门控信号'}"
        )

    rerank_evidence = ranking_evidence.get("rerank_evidence") or {}
    if rerank_score > 0 or rerank_evidence:
        provider = rerank_evidence.get("provider") if isinstance(rerank_evidence, dict) else None
        provider_text = f"（{provider}）" if provider else ""
        reasons.append(f"重排模型{provider_text}给出 rerank_score: {rerank_score:.4f}，用于最终排序校准")

    return _dedupe(reasons)


def _generate_follow_up_questions(intent: Dict[str, Any]) -> List[str]:
    query_understanding = intent.get("query_understanding", {})
    research_interests = query_understanding.get("research_interests") or []
    if intent.get("intent") != "general_recommendation" and research_interests:
        return []

    questions: List[str] = []
    if not research_interests:
        questions.append("你更倾向于理论研究还是应用研究？")
    if not query_understanding.get("preferred_universities"):
        questions.append("是否只考虑 985 高校？")
    if not query_understanding.get("degree_stage"):
        questions.append("你计划申请硕士还是博士？")
    if not query_understanding.get("preferred_locations"):
        questions.append("你对地区有偏好吗？")
    return questions


def _load_json_object(value: str | None) -> Dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _load_json_list(value: str | None) -> List[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _merge_unique(old_values: Any, new_values: Any) -> List[Any]:
    result: List[Any] = []
    seen = set()
    for value in [*_as_list(old_values), *_as_list(new_values)]:
        marker = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else value
        if marker in seen:
            continue
        seen.add(marker)
        result.append(value)
    return result


def _merge_follow_up_intent(
    previous_intent: Dict[str, Any],
    new_intent: Dict[str, Any],
) -> Dict[str, Any]:
    merged = {**previous_intent, **new_intent}
    previous_understanding = previous_intent.get("query_understanding") or {}
    new_understanding = new_intent.get("query_understanding") or {}
    merged_understanding = {**previous_understanding, **new_understanding}

    for key in ["research_interests", "preferred_locations", "preferred_universities"]:
        merged_understanding[key] = _merge_unique(
            previous_understanding.get(key),
            new_understanding.get(key),
        )
    merged_understanding["constraints"] = _merge_unique(
        previous_understanding.get("constraints"),
        new_understanding.get("constraints"),
    )
    if not merged_understanding["constraints"]:
        merged_understanding["soft_preferences"] = _merge_unique(
            previous_understanding.get("soft_preferences"),
            new_understanding.get("soft_preferences") or [new_intent.get("query")],
        )

    for key in ["degree_stage", "exclusions"]:
        if key in new_understanding:
            merged_understanding[key] = new_understanding.get(key)
        elif key in previous_understanding:
            merged_understanding[key] = previous_understanding.get(key)

    merged["query_understanding"] = merged_understanding
    merged["keywords"] = _merge_unique(
        previous_intent.get("keywords"),
        [*(_as_list(new_intent.get("keywords"))), *merged_understanding.get("research_interests", [])],
    )
    merged["tags"] = _merge_unique(previous_intent.get("tags"), new_intent.get("tags"))
    return merged


def _added_constraints(
    previous_intent: Dict[str, Any],
    new_intent: Dict[str, Any],
) -> List[Any]:
    previous_constraints = (previous_intent.get("query_understanding") or {}).get("constraints")
    new_constraints = (new_intent.get("query_understanding") or {}).get("constraints")
    previous_set = set(_as_list(previous_constraints))
    return [constraint for constraint in _as_list(new_constraints) if constraint not in previous_set]


def _item_rank_changes(
    previous_item_ids: List[Any],
    current_item_ids: List[Any],
) -> Dict[str, List[Any]]:
    previous_rank = {item_id: index for index, item_id in enumerate(previous_item_ids)}
    current_rank = {item_id: index for index, item_id in enumerate(current_item_ids)}
    shared_item_ids = [item_id for item_id in current_item_ids if item_id in previous_rank]
    return {
        "promoted_item_ids": [
            item_id for item_id in shared_item_ids if current_rank[item_id] < previous_rank[item_id]
        ] + [item_id for item_id in current_item_ids if item_id not in previous_rank],
        "demoted_item_ids": [
            item_id for item_id in shared_item_ids if current_rank[item_id] > previous_rank[item_id]
        ],
        "removed_item_ids": [item_id for item_id in previous_item_ids if item_id not in current_rank],
    }


def _follow_up_explanation(changes: Dict[str, Any]) -> str:
    added_constraints = changes["added_constraints"]
    constraint_text = "、".join(str(item) for item in added_constraints) or "追问条件"
    return (
        f"根据您新增的{constraint_text}，重新调整了推荐结果。"
        f"新增了{len(changes['promoted_item_ids'])}位导师，"
        f"移除了{len(changes['removed_item_ids'])}位导师。"
    )


class RecommendationService:
    """Main recommendation service orchestrating the full pipeline"""

    def __init__(self):
        self.retrieval_service = RetrievalService()
        self.ranking_service = RankingService()
        self.reranker_service = RerankerService()
        self.llm_service = LLMService()
        self.graph_service = GraphService()

    async def recommend(
        self,
        query: str = "",
        user_id: Optional[int] = None,
        top_k: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
        intent_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Full recommendation pipeline"""
        request_id = f"req_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        start_time = time.time()

        top_k = top_k or settings.DEFAULT_TOP_K
        options = options or {}
        filters = filters or {}

        # Step 1: Parse intent
        intent = intent_override or await self.llm_service.parse_intent(query)

        # Step 2: Retrieve expanded candidates
        candidates = self.retrieval_service.retrieve_candidates(
            query=query,
            keywords=intent.get("keywords", []),
            org_unit=filters.get("org_unit") or intent.get("org_unit"),
            title=filters.get("title"),
            top_k=top_k * 3,
        )

        # Step 3: Rank candidates into an initial shortlist for optional reranking.
        shortlist = self.ranking_service.rank_candidates(
            candidates,
            top_k=settings.RERANK_TOP_N,
        )

        # Step 4: Fetch full item details, then generate explanations concurrently.
        recommendation_payloads = []
        item_context_by_id: Dict[Any, Dict[str, Any]] = {}
        with Session(engine) as session:
            for rc in shortlist:
                item = session.exec(select(Item).where(Item.id == rc["item_id"])).first()
                if not item:
                    continue
                org_unit = None
                if item.org_unit:
                    org_unit = session.exec(
                        select(OrgUnit).where(OrgUnit.name == item.org_unit)
                    ).first()
                item_context_by_id[rc["item_id"]] = {
                    "item": item,
                    "org_unit": org_unit,
                    "rerank_payload": self._build_rerank_payload(rc, item, org_unit),
                }

            if settings.ENABLE_RERANKER:
                rerank_payloads = [
                    item_context_by_id[rc["item_id"]]["rerank_payload"]
                    for rc in shortlist
                    if rc["item_id"] in item_context_by_id
                ]
                rerank_results = self.reranker_service.rerank(query, rerank_payloads)
                ranked = self.ranking_service.apply_rerank_scores(
                    shortlist,
                    rerank_results,
                    top_k=top_k,
                )
            else:
                ranked = shortlist[:top_k]

            for rc in ranked:
                item_context = item_context_by_id.get(rc["item_id"])
                if not item_context:
                    continue
                item = item_context["item"]
                evidence = {
                    "semantic_score": rc.get("semantic_score", 0),
                    "graph_score": rc.get("graph_score", 0),
                    "profile_score": rc.get("profile_score", 0),
                    "popularity_score": rc.get("popularity_score", 0),
                }
                for key in [
                    "domain_score",
                    "rerank_score",
                    "domain_evidence",
                    "rerank_evidence",
                ]:
                    if key in rc:
                        evidence[key] = rc[key]

                recommendation_payloads.append({
                    "item_id": item.id,
                    "title": item.title,
                    "category": item.category,
                    "org_unit": item.org_unit,
                    "tags": item.tags,
                    "research_areas": item.research_areas,
                    "description": item.description,
                    "score": rc["final_score"],
                    "evidence": evidence,
                    "sources": rc.get("sources", []),
                    "graph_paths": [],
                })

        explanations = await asyncio.gather(
            *(
                self._generate_recommendation_explanation(
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
        recommendations.sort(key=lambda recommendation: recommendation["score"] or 0, reverse=True)
        graph_paths = self._build_graph_paths(recommendations)

        latency_ms = int((time.time() - start_time) * 1000)

        # Step 5: Log recommendation
        log = RecommendationLog(
            request_id=request_id,
            user_id=user_id,
            query=query,
            strategy="hybrid",
            candidate_count=len(shortlist),
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

    async def _generate_recommendation_explanation(
        self,
        item_title: str,
        query: str,
        graph_paths: List[Dict[str, Any]],
    ) -> str:
        """Generate the original recommendation reason with a safe test fallback."""
        generate_explanation = getattr(self.llm_service, "generate_explanation", None)
        if generate_explanation is None:
            return f"{item_title} 与您的查询“{query}”相关，推荐进一步了解。"
        return await generate_explanation(
            item_title=item_title,
            query=query,
            graph_paths=graph_paths,
        )

    def _build_rerank_payload(
        self,
        candidate: Dict[str, Any],
        item: Item,
        org_unit: OrgUnit | None,
    ) -> Dict[str, Any]:
        """Build an offline reranker payload from an already-filtered shortlist item."""
        org_unit_name = org_unit.name if org_unit else item.org_unit
        return {
            **candidate,
            "name": item.title,
            "org_unit": org_unit_name,
            "research_areas": item.research_areas,
            "summary": item.description,
            "tags": item.tags,
        }

    def _build_ranking_evidence(self, candidate: Dict[str, Any]) -> Dict[str, Any]:
        """Keep ranking/domain/graph/reranker evidence in the existing evidence payload."""
        return {
            "semantic_score": candidate.get("semantic_score", 0.0),
            "graph_score": candidate.get("graph_score", 0.0),
            "profile_score": candidate.get("profile_score", 0.0),
            "popularity_score": candidate.get("popularity_score", 0.0),
            "domain_score": candidate.get("domain_score", 0.0),
            "rerank_score": candidate.get("rerank_score", 0.0),
            "final_score": candidate.get("final_score", 0.0),
            "domain": candidate.get("domain"),
            "domain_match": candidate.get("domain_match"),
            "domain_confidence": candidate.get("domain_confidence"),
            "domain_gate_confidence": candidate.get("domain_gate_confidence"),
            "domain_evidence": candidate.get("domain_evidence", []),
            "domain_fallback_reason": candidate.get("domain_fallback_reason"),
            "graph_evidence": candidate.get("graph_evidence", []),
            "path": candidate.get("path", []),
            "relations": candidate.get("relations", []),
            "contributing_terms": candidate.get("contributing_terms", []),
            "rerank_evidence": candidate.get("rerank_evidence", {}),
        }

    async def follow_up(
        self,
        previous_request_id: str,
        query: str,
        user_id: Optional[int] = None,
        top_k: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Continue a recommendation session by merging new constraints into prior intent."""
        with Session(engine) as session:
            previous_log = session.exec(
                select(RecommendationLog).where(
                    RecommendationLog.request_id == previous_request_id
                )
            ).first()

        if previous_log is None:
            return error_response(
                code=40404,
                message="未找到可追问的推荐上下文",
                request_id=previous_request_id,
            )

        debug_json = _load_json_object(previous_log.debug_json)
        previous_intent = debug_json.get("intent") or {}
        if not isinstance(previous_intent, dict):
            previous_intent = {}

        new_intent = await self.llm_service.parse_intent(query)
        if not isinstance(new_intent, dict):
            new_intent = {}

        merged_intent = _merge_follow_up_intent(previous_intent, new_intent)
        recommendation_response = await self.recommend(
            query=query,
            user_id=user_id or previous_log.user_id,
            top_k=top_k,
            filters=filters,
            options=options,
            intent_override=merged_intent,
        )

        if recommendation_response.get("code") != 0:
            return recommendation_response

        data = recommendation_response.get("data") or {}
        current_item_ids = [
            recommendation.get("item_id")
            for recommendation in data.get("recommendations", [])
            if recommendation.get("item_id") is not None
        ]
        changes: Dict[str, Any] = {
            "added_constraints": _added_constraints(previous_intent, new_intent),
            **_item_rank_changes(
                _load_json_list(previous_log.result_item_ids),
                current_item_ids,
            ),
        }
        changes["explanation"] = _follow_up_explanation(changes)
        data["changes"] = changes
        recommendation_response["data"] = data
        return recommendation_response

    def _build_item_graph_paths(
        self,
        item: Item,
        org_unit: OrgUnit | None,
        ranking_evidence: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        if not org_unit and not item.org_unit:
            return []
        target_label = org_unit.name if org_unit else item.org_unit
        return [
            {
                "source": f"Item_{item.id}",
                "source_label": item.title,
                "source_type": "Item",
                "target": f"Org_{target_label}",
                "target_label": target_label,
                "target_type": "OrgUnit",
                "relation": "belongs_to",
                "weight": ranking_evidence.get("graph_score", 0),
            }
        ]

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
                "source_label": recommendation.get("name") or recommendation.get("title", source),
                "source_type": "Item",
                "target": target,
                "target_label": org_unit,
                "target_type": "OrgUnit",
                "relation": "belongs_to",
                "weight": 1.0,
            })

        return graph_paths
