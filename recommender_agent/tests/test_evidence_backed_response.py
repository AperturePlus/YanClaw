import json
from datetime import datetime

import pytest

from app.models.item import Item
from app.models.org_unit import OrgUnit
from app.services.ranking_service import RankingService
from app.services.recommendation_service import RecommendationService


class StubLLMService:
    def __init__(self, intent):
        self.intent = intent

    async def parse_intent(self, query: str):
        return self.intent


class StubRetrievalService:
    def __init__(self, candidates):
        self.candidates = candidates

    def retrieve_candidates(self, **kwargs):
        return self.candidates


class StubRankingService:
    def __init__(self, ranked):
        self.ranked = ranked
        self.rank_top_k = None
        self.apply_top_k = None

    def rank_candidates(self, candidates, top_k: int = 10):
        self.rank_top_k = top_k
        return self.ranked[:top_k]

    def apply_rerank_scores(self, shortlist, rerank_results, top_k: int = 10):
        self.apply_top_k = top_k
        return RankingService().apply_rerank_scores(shortlist, rerank_results, top_k=top_k)


class StubRerankerService:
    def __init__(self, results):
        self.results = results
        self.query = None
        self.payloads = None

    def rerank(self, query: str, candidates):
        self.query = query
        self.payloads = candidates
        return self.results


@pytest.mark.asyncio
async def test_recommend_returns_evidence_backed_envelope_and_required_shape(
    db_session,
    isolated_app_state,
):
    item = Item(
        title="张三",
        category="计算机学院",
        description="长期从事自然语言处理与知识图谱研究。",
        research_areas="自然语言处理、知识图谱",
        org_unit="计算机学院",
        tags="教授",
        popularity=0.8,
        metadata_json=json.dumps(
            {
                "homepage": "https://cs.example.edu.cn/zhangsan",
                "source_url": "https://cs.example.edu.cn/faculty/zhangsan",
                "updated_at": "2026-01-02",
                "source": "scu.edu.cn.db",
                "university": "四川大学",
            },
            ensure_ascii=False,
        ),
        created_at=datetime(2026, 1, 3, 4, 5, 6),
    )
    db_session.add(item)
    db_session.add(OrgUnit(name="计算机学院", url="https://cs.example.edu.cn"))
    db_session.commit()
    db_session.refresh(item)

    service = object.__new__(RecommendationService)
    setattr(service, "llm_service", StubLLMService(
        {
            "intent": "professor_recommendation",
            "keywords": ["自然语言处理"],
            "tags": ["自然语言处理"],
            "org_unit": "计算机学院",
            "title": "教授",
            "query_understanding": {
                "research_interests": ["自然语言处理"],
                "preferred_locations": [],
                "preferred_universities": [],
                "degree_stage": "",
                "constraints": ["学院/机构：计算机学院", "职称/导师类型：教授"],
                "soft_preferences": [],
                "exclusions": [],
                "uncertainties": [],
                "follow_up_intent": None,
            },
        }
    ))
    ranked = [
        {
            "item_id": item.id,
            "final_score": 0.86,
            "semantic_score": 0.91,
            "graph_score": 0.63,
            "profile_score": 0.5,
            "popularity_score": 0.4,
            "sources": ["vector", "graph", "structured"],
        }
    ]
    setattr(service, "retrieval_service", StubRetrievalService(ranked))
    setattr(service, "ranking_service", StubRankingService(ranked))
    setattr(service, "graph_service", None)

    response = await service.recommend(query="自然语言处理方向的计算机学院教授", top_k=1)

    assert response["code"] == 0
    assert response["request_id"]
    assert response["data"]["request_id"] == response["request_id"]
    assert response["data"]["query_understanding"] == {
        "intent": "professor_recommendation",
        "keywords": ["自然语言处理"],
        "tags": ["自然语言处理"],
    }
    recommendation = response["data"]["recommendations"][0]
    for field in [
        "item_id",
        "title",
        "category",
        "org_unit",
        "tags",
        "research_areas",
        "description",
        "score",
        "evidence",
        "sources",
        "graph_paths",
        "reason",
    ]:
        assert field in recommendation
    assert recommendation["item_id"] == item.id
    assert recommendation["title"] == "张三"
    assert recommendation["score"] == 0.86
    assert recommendation["sources"] == ["vector", "graph", "structured"]
    assert recommendation["evidence"]["semantic_score"] == 0.91
    assert response["data"]["graph_paths"][0]["relation"] == "belongs_to"


@pytest.mark.asyncio
async def test_recommend_weak_evidence_returns_limitations_and_follow_up_questions(
    db_session,
    isolated_app_state,
):
    item = Item(
        title="李四",
        category="",
        description=None,
        research_areas="",
        org_unit=None,
        tags=None,
        metadata_json=json.dumps({"source": "scu.edu.cn.db"}, ensure_ascii=False),
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    service = object.__new__(RecommendationService)
    setattr(service, "llm_service", StubLLMService(
        {
            "intent": "general_recommendation",
            "keywords": ["推荐一下"],
            "tags": [],
            "org_unit": None,
            "title": None,
            "query_understanding": {
                "research_interests": [],
                "preferred_locations": [],
                "preferred_universities": [],
                "degree_stage": "",
                "constraints": [],
                "soft_preferences": [],
                "exclusions": [],
                "uncertainties": ["用户需求较宽泛，需要结合推荐结果继续澄清偏好"],
                "follow_up_intent": None,
            },
        }
    ))
    ranked = [
        {
            "item_id": item.id,
            "final_score": 0.49,
            "semantic_score": 0,
            "graph_score": 0,
            "profile_score": 0.2,
            "popularity_score": 0,
            "sources": ["structured"],
        }
    ]
    setattr(service, "retrieval_service", StubRetrievalService(ranked))
    setattr(service, "ranking_service", StubRankingService(ranked))
    setattr(service, "graph_service", None)

    response = await service.recommend(query="推荐一下", top_k=1)

    recommendation = response["data"]["recommendations"][0]
    assert recommendation["score"] < 0.5
    assert recommendation["title"] == "李四"
    assert recommendation["sources"] == ["structured"]


@pytest.mark.asyncio
async def test_recommend_reranks_shortlist_and_preserves_evidence_shape(
    db_session,
    isolated_app_state,
):
    isolated_app_state.ENABLE_RERANKER = True
    isolated_app_state.RERANK_TOP_N = 2

    first_item = Item(
        title="语义检索导师",
        category="计算机学院",
        description="从事自然语言处理研究。",
        research_areas="自然语言处理",
        org_unit="计算机学院",
        tags="教授",
        metadata_json=json.dumps({"source": "fixture", "university": "测试大学"}, ensure_ascii=False),
    )
    second_item = Item(
        title="重排优先导师",
        category="计算机学院",
        description="从事自然语言处理、知识图谱和推荐系统研究。",
        research_areas="自然语言处理、知识图谱、推荐系统",
        org_unit="计算机学院",
        tags="教授",
        metadata_json=json.dumps({"source": "fixture", "university": "测试大学"}, ensure_ascii=False),
    )
    db_session.add(first_item)
    db_session.add(second_item)
    db_session.add(OrgUnit(name="计算机学院", url="https://cs.example.edu.cn"))
    db_session.commit()
    db_session.refresh(first_item)
    db_session.refresh(second_item)

    intent = {
        "intent": "professor_recommendation",
        "keywords": ["自然语言处理"],
        "tags": ["自然语言处理"],
        "org_unit": "计算机学院",
        "title": "教授",
        "query_understanding": {
            "research_interests": ["自然语言处理"],
            "preferred_locations": [],
            "preferred_universities": [],
            "degree_stage": "",
            "constraints": [],
            "soft_preferences": [],
            "exclusions": [],
            "uncertainties": [],
            "follow_up_intent": None,
        },
    }
    ranked = [
        {
            "item_id": first_item.id,
            "final_score": 0.7,
            "semantic_score": 0.9,
            "graph_score": 0.2,
            "profile_score": 0.2,
            "popularity_score": 0.0,
            "domain_score": 0.2,
            "domain": "computer_information",
            "domain_match": True,
            "domain_evidence": ["research_areas:自然语言处理"],
            "graph_evidence": [{"path": ["ResearchArea_NLP", f"Item_{first_item.id}"]}],
            "path": ["ResearchArea_NLP", f"Item_{first_item.id}"],
            "relations": ["has_research_area"],
            "contributing_terms": ["NLP"],
            "sources": ["vector", "graph"],
        },
        {
            "item_id": second_item.id,
            "final_score": 0.6,
            "semantic_score": 0.7,
            "graph_score": 0.5,
            "profile_score": 0.2,
            "popularity_score": 0.0,
            "domain_score": 0.8,
            "domain": "computer_information",
            "domain_match": True,
            "domain_evidence": ["research_areas:知识图谱"],
            "graph_evidence": [{"path": ["ResearchArea_KG", f"Item_{second_item.id}"]}],
            "path": ["ResearchArea_KG", f"Item_{second_item.id}"],
            "relations": ["has_research_area"],
            "contributing_terms": ["知识图谱"],
            "sources": ["vector", "graph", "structured"],
        },
    ]
    ranking_service = StubRankingService(ranked)
    reranker_service = StubRerankerService(
        [
            {
                "item_id": first_item.id,
                "rerank_score": 0.0,
                "rerank_evidence": {"provider": "heuristic", "reason": "weaker profile"},
            },
            {
                "item_id": second_item.id,
                "rerank_score": 1.0,
                "rerank_evidence": {"provider": "heuristic", "reason": "stronger profile"},
            },
        ]
    )

    service = object.__new__(RecommendationService)
    setattr(service, "llm_service", StubLLMService(intent))
    setattr(service, "retrieval_service", StubRetrievalService(ranked))
    setattr(service, "ranking_service", ranking_service)
    setattr(service, "reranker_service", reranker_service)
    setattr(service, "graph_service", None)

    response = await service.recommend(query="自然语言处理方向的计算机学院教授", top_k=1)

    assert response["code"] == 0
    assert ranking_service.rank_top_k == 2
    assert ranking_service.apply_top_k == 1
    assert reranker_service.payloads is not None
    assert reranker_service.payloads[1]["name"] == "重排优先导师"
    recommendation = response["data"]["recommendations"][0]
    assert recommendation["item_id"] == second_item.id
    assert "evidence" in recommendation
    assert recommendation["evidence"]["domain_score"] == 0.8
    assert recommendation["evidence"]["domain_evidence"] == ["research_areas:知识图谱"]
    assert recommendation["evidence"]["rerank_score"] == 1.0
    assert recommendation["evidence"]["rerank_evidence"]["provider"] == "heuristic"
    assert "重排优先导师" in recommendation["reason"]
