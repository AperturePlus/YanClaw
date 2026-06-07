from typing import Any

import pytest

from app.core.config import Settings
from app.services.reranker_service import (
    ExternalRerankerProvider,
    HeuristicRerankerProvider,
    RerankerService,
)


def _candidate(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "item_id": 1,
        "name": "张三",
        "org_unit": "计算机学院",
        "research_areas": "自然语言处理、知识图谱",
        "summary": "长期从事 NLP 和推荐系统研究",
        "tags": ["教授", "博导"],
        "graph_score": 0.8,
        "sources": ["vector", "graph"],
    }
    base.update(overrides)
    return base


def test_disabled_reranker_returns_neutral_scores():
    settings = Settings(ENABLE_RERANKER=False, RERANK_PROVIDER="heuristic")
    service = RerankerService(config=settings)

    reranked = service.rerank("自然语言处理 博导", [_candidate()])

    assert reranked[0]["rerank_score"] == 0.0
    assert reranked[0]["rerank_evidence"] == {
        "provider": "disabled",
        "reason": "reranker disabled",
    }


def test_disabled_provider_mode_returns_neutral_scores_when_enabled():
    settings = Settings(ENABLE_RERANKER=True, RERANK_PROVIDER="disabled")
    service = RerankerService(config=settings)

    reranked = service.rerank("自然语言处理", [_candidate()])

    assert reranked[0]["rerank_score"] == 0.0
    assert reranked[0]["rerank_evidence"]["provider"] == "disabled"


def test_heuristic_reranker_scores_query_domain_and_graph_evidence():
    settings = Settings(ENABLE_RERANKER=True, RERANK_PROVIDER="heuristic")
    service = RerankerService(config=settings)
    strong = _candidate(item_id=1)
    weak = _candidate(
        item_id=2,
        name="李四",
        org_unit="物理学院",
        research_areas="凝聚态物理",
        summary="材料物性研究",
        tags=["副教授"],
        graph_score=0.0,
        sources=[],
    )

    reranked = service.rerank("自然语言处理 知识图谱 计算机学院 博导", [strong, weak])

    assert reranked[0]["rerank_score"] > reranked[1]["rerank_score"]
    assert reranked[0]["rerank_score"] > 0.6
    assert reranked[0]["rerank_evidence"]["provider"] == "heuristic"
    assert "research_areas" in reranked[0]["rerank_evidence"]["matched_fields"]
    assert "org_unit" in reranked[0]["rerank_evidence"]["matched_fields"]


def test_heuristic_reranker_respects_top_n_and_preserves_tail():
    settings = Settings(ENABLE_RERANKER=True, RERANK_PROVIDER="heuristic", RERANK_TOP_N=1)
    service = RerankerService(config=settings)

    reranked = service.rerank("自然语言处理", [_candidate(item_id=1), _candidate(item_id=2)])

    assert reranked[0]["rerank_evidence"]["provider"] == "heuristic"
    assert reranked[1]["rerank_score"] == 0.0
    assert reranked[1]["rerank_evidence"]["provider"] == "not_reranked"


def test_external_provider_missing_config_falls_back_to_heuristic(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        ENABLE_RERANKER=True,
        RERANK_PROVIDER="external-test",
        RERANK_MODEL="",
        RERANK_BASE_URL="",
        RERANK_API_KEY="",
    )

    def fail_if_called(
        self: ExternalRerankerProvider,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        _ = (self, query, candidates)
        raise AssertionError("external transport should not be called when config is missing")

    monkeypatch.setattr(ExternalRerankerProvider, "_call_provider", fail_if_called)

    reranked = RerankerService(config=settings).rerank("自然语言处理", [_candidate()])

    assert reranked[0]["rerank_score"] > 0
    assert reranked[0]["rerank_evidence"]["provider"] == "heuristic"


def test_external_provider_malformed_response_falls_back_to_heuristic(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        ENABLE_RERANKER=True,
        RERANK_PROVIDER="external-test",
        RERANK_MODEL="rerank-model",
        RERANK_BASE_URL="https://reranker.invalid/v1",
        RERANK_API_KEY="test-key",
    )

    monkeypatch.setattr(
        ExternalRerankerProvider,
        "_call_provider",
        _malformed_provider_response,
    )

    reranked = RerankerService(config=settings).rerank("自然语言处理", [_candidate()])

    assert reranked[0]["rerank_score"] > 0
    assert reranked[0]["rerank_evidence"]["provider"] == "heuristic"


def test_external_provider_normalizes_valid_provider_response(monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        ENABLE_RERANKER=True,
        RERANK_PROVIDER="external-test",
        RERANK_MODEL="rerank-model",
        RERANK_BASE_URL="https://reranker.invalid/v1",
        RERANK_API_KEY="test-key",
    )

    monkeypatch.setattr(
        ExternalRerankerProvider,
        "_call_provider",
        _valid_provider_response,
    )

    reranked = RerankerService(config=settings).rerank("自然语言处理", [_candidate()])

    assert reranked[0]["rerank_score"] == 1.0
    assert reranked[0]["rerank_evidence"] == {
        "provider": "external-test",
        "reason": "provider match",
    }


def test_heuristic_provider_returns_result_for_each_candidate():
    provider = HeuristicRerankerProvider()
    results = provider.rerank("", [_candidate(item_id=1), _candidate(item_id=2)])

    assert [result.item_id for result in results] == [1, 2]
    assert all(result.rerank_score >= 0 for result in results)


def _malformed_provider_response(
    self: ExternalRerankerProvider,
    query: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    _ = (self, query)
    return [{"item_id": candidates[0]["item_id"]}]


def _valid_provider_response(
    self: ExternalRerankerProvider,
    query: str,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    _ = (self, query)
    return [
        {
            "item_id": candidates[0]["item_id"],
            "rerank_score": 1.7,
            "rerank_evidence": {"reason": "provider match"},
        }
    ]
