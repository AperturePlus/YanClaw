import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.api.v1 import recommend as recommend_api
from app.main import app
from app.models.item import Item
from app.models.recommendation_log import RecommendationLog


client = TestClient(app)


@pytest.mark.usefixtures("isolated_app_state")
def test_follow_up_returns_enriched_response_with_changes(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous_intent = {
        "intent": "professor_recommendation",
        "keywords": ["NLP"],
        "tags": ["NLP"],
        "org_unit": None,
        "title": None,
        "query_understanding": {
            "research_interests": ["NLP"],
            "preferred_locations": [],
            "preferred_universities": [],
            "degree_stage": "",
            "constraints": ["研究方向：NLP"],
            "soft_preferences": [],
            "exclusions": [],
            "uncertainties": [],
            "follow_up_intent": None,
        },
    }
    db_session.add(
        RecommendationLog(
            request_id="req_previous",
            user_id=7,
            query="NLP方向导师推荐",
            strategy="hybrid",
            candidate_count=1,
            result_item_ids=json.dumps([1]),
            latency_ms=10,
            debug_json=json.dumps({"intent": previous_intent}, ensure_ascii=False),
        )
    )
    db_session.add(
        Item(
            id=2,
            title="李老师",
            category="计算机学院",
            description="研究自然语言处理和知识图谱。",
            research_areas="自然语言处理、知识图谱",
            org_unit="计算机学院",
            tags="教授",
            popularity=0.9,
            metadata_json=json.dumps({"university": "四川大学"}, ensure_ascii=False),
        )
    )
    db_session.commit()

    async def parse_intent(query: str) -> dict[str, object]:
        return {
            "intent": "constrained_recommendation",
            "keywords": ["自然语言处理", "成都"],
            "tags": ["自然语言处理"],
            "org_unit": None,
            "title": None,
            "query_understanding": {
                "research_interests": ["自然语言处理"],
                "preferred_locations": ["成都"],
                "preferred_universities": [],
                "degree_stage": "",
                "constraints": ["地区偏好：成都"],
                "soft_preferences": [],
                "exclusions": [],
                "uncertainties": [],
                "follow_up_intent": "refine",
            },
        }

    monkeypatch.setattr(recommend_api.rec_service.llm_service, "parse_intent", parse_intent)
    monkeypatch.setattr(
        recommend_api.rec_service.retrieval_service,
        "retrieve_candidates",
        lambda **_: [
            {
                "item_id": 2,
                "semantic_score": 0.8,
                "graph_score": 0.0,
                "profile_score": 0.5,
                "popularity_score": 0.45,
                "sources": ["structured"],
            }
        ],
    )
    monkeypatch.setattr(
        recommend_api.rec_service.ranking_service,
        "rank_candidates",
        lambda candidates, top_k=10: [
            {**candidates[0], "final_score": 0.91}
        ],
    )

    response = client.post(
        "/api/v1/recommend/follow-up",
        json={
            "previous_request_id": "req_previous",
            "query": "希望在成都",
            "top_k": 1,
            "filters": {},
            "options": {},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 0
    changes = payload["data"]["changes"]
    assert changes["added_constraints"] == ["地区偏好：成都"]
    assert changes["promoted_item_ids"] == [2]
    assert changes["demoted_item_ids"] == []
    assert changes["removed_item_ids"] == [1]
    assert "根据您新增的地区偏好：成都" in changes["explanation"]
    assert payload["data"]["recommendations"][0]["item_id"] == 2


@pytest.mark.usefixtures("isolated_app_state")
def test_follow_up_invalid_previous_request_id_returns_error() -> None:
    response = client.post(
        "/api/v1/recommend/follow-up",
        json={"previous_request_id": "missing", "query": "继续追问"},
    )

    assert response.status_code == 404
    payload = response.json()
    assert payload["code"] == 40404
    assert payload["message"] == "未找到可追问的推荐上下文"
