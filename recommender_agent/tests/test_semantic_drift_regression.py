from typing import Any

from sqlmodel import Session

from app.models import GraphEdge, Item
from app.services.ranking_service import RankingService
from app.services.reranker_service import RerankerService


class _FakeVectorService:
    def __init__(self, results: list[dict[str, object]]) -> None:
        self.results = results

    def search_similar(self, query: str, top_k: int = 10) -> list[dict[str, object]]:
        return self.results[:top_k]


def _add_item(
    session: Session,
    *,
    title: str,
    org_unit: str,
    research_areas: str,
    tags: str = "教授",
    description: str | None = None,
    popularity: float = 0.8,
) -> Item:
    item = Item(
        title=title,
        category=org_unit,
        org_unit=org_unit,
        research_areas=research_areas,
        tags=tags,
        description=description or research_areas,
        popularity=popularity,
        is_active=True,
    )
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _add_edge(
    session: Session,
    source_id: str,
    source_type: str,
    target_id: str,
    target_type: str,
    relation: str,
    *,
    weight: float = 1.0,
    confidence: float = 1.0,
    evidence: str | None = None,
) -> None:
    session.add(
        GraphEdge(
            source_id=source_id,
            source_type=source_type,
            target_id=target_id,
            target_type=target_type,
            relation=relation,
            weight=weight,
            confidence=confidence,
            evidence=evidence,
        )
    )


def _configure_regression_settings(settings: Any) -> None:
    settings.ENABLE_VECTOR = True
    settings.ENABLE_GRAPH = True
    settings.GRAPH_DIFFUSION_ENABLED = True
    settings.ENABLE_DOMAIN_GATE = True
    settings.DOMAIN_GATE_MODE = "soft"
    settings.ENABLE_RERANKER = True
    settings.RERANK_PROVIDER = "heuristic"
    settings.RERANK_TOP_N = 10
    settings.GRAPH_MAX_DEPTH = 2


def _semantic_drift_fixture(session: Session) -> tuple[Item, Item]:
    nlp_item = _add_item(
        session,
        title="自然语言处理导师",
        org_unit="计算机学院",
        research_areas="自然语言处理、知识图谱",
        description="长期从事 NLP、自然语言处理、知识图谱和信息检索研究。",
        popularity=0.8,
    )
    chemistry_item = _add_item(
        session,
        title="分析化学导师",
        org_unit="化学学院",
        research_areas="分析化学、催化",
        description="主要研究分析化学、催化材料与谱学分析。",
        popularity=0.8,
    )
    _add_edge(
        session,
        f"Item_{nlp_item.id}",
        "Item",
        "ResearchArea_NLP",
        "ResearchArea",
        "has_research_area",
        evidence="profile states NLP research area",
    )
    _add_edge(
        session,
        f"Item_{nlp_item.id}",
        "Item",
        "ResearchArea_KnowledgeGraph",
        "ResearchArea",
        "has_research_area",
        evidence="profile states knowledge graph research area",
    )
    _add_edge(
        session,
        f"Item_{chemistry_item.id}",
        "Item",
        "ResearchArea_分析化学",
        "ResearchArea",
        "has_research_area",
        evidence="profile states analysis chemistry research area",
    )
    session.commit()
    return nlp_item, chemistry_item


def _retrieve_then_rank(
    query: str,
    keywords: list[str],
    *,
    settings: Any,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    from app.services.retrieval_service import RetrievalService

    candidates = RetrievalService().retrieve_candidates(
        query=query,
        keywords=keywords,
        top_k=top_k,
    )
    initial = RankingService().rank_candidates(candidates, top_k=top_k)
    reranked = RerankerService(config=settings).rerank(query, initial)
    return RankingService().apply_rerank_scores(initial, reranked, top_k=top_k)


def test_nlp_query_corrects_misleading_semantic_drift_with_domain_graph_and_rerank(
    isolated_app_state,
    db_session: Session,
    monkeypatch,
) -> None:
    _configure_regression_settings(isolated_app_state)
    nlp_item, chemistry_item = _semantic_drift_fixture(db_session)

    monkeypatch.setattr(
        "app.services.retrieval_service.VectorService",
        lambda: _FakeVectorService(
            [
                {"item_id": chemistry_item.id, "score": 0.92, "source": "vector"},
                {"item_id": nlp_item.id, "score": 0.91, "source": "vector"},
            ]
        ),
    )

    ranked = _retrieve_then_rank(
        "NLP方向导师推荐",
        ["NLP"],
        settings=isolated_app_state,
        top_k=2,
    )

    assert [candidate["item_id"] for candidate in ranked] == [nlp_item.id, chemistry_item.id]
    nlp_candidate = ranked[0]
    chemistry_candidate = ranked[1]
    assert chemistry_candidate["semantic_score"] > nlp_candidate["semantic_score"]
    assert nlp_candidate["domain"] == "computer_information"
    assert nlp_candidate["domain_match"] is True
    assert nlp_candidate["domain_score"] > chemistry_candidate["domain_score"]
    assert nlp_candidate["graph_score"] > chemistry_candidate["graph_score"]
    assert nlp_candidate["graph_evidence"]
    assert nlp_candidate["path"] == ["ResearchArea_NLP", f"Item_{nlp_item.id}"]
    assert nlp_candidate["relations"] == ["has_research_area"]
    assert nlp_candidate["rerank_score"] > chemistry_candidate["rerank_score"]
    assert nlp_candidate["rerank_evidence"]["provider"] == "heuristic"
    assert nlp_candidate["final_score"] > chemistry_candidate["final_score"]
    assert {"vector", "graph", "structured"}.issubset(set(nlp_candidate["sources"]))


def test_unknown_domain_query_keeps_non_empty_structured_results(
    isolated_app_state,
    db_session: Session,
    monkeypatch,
) -> None:
    _configure_regression_settings(isolated_app_state)
    nlp_item, chemistry_item = _semantic_drift_fixture(db_session)
    monkeypatch.setattr(
        "app.services.retrieval_service.VectorService",
        lambda: _FakeVectorService([]),
    )

    ranked = _retrieve_then_rank(
        "帮我推荐合适导师",
        [],
        settings=isolated_app_state,
        top_k=5,
    )

    assert {candidate["item_id"] for candidate in ranked} == {nlp_item.id, chemistry_item.id}
    assert all(candidate["domain"] == "general_unknown" for candidate in ranked)
    assert all(candidate["domain_score"] == 0.0 for candidate in ranked)
    assert all("structured" in candidate["sources"] for candidate in ranked)


def test_interdisciplinary_query_keeps_computer_and_chemistry_candidates(
    isolated_app_state,
    db_session: Session,
    monkeypatch,
) -> None:
    _configure_regression_settings(isolated_app_state)
    nlp_item, chemistry_item = _semantic_drift_fixture(db_session)
    monkeypatch.setattr(
        "app.services.retrieval_service.VectorService",
        lambda: _FakeVectorService(
            [
                {"item_id": nlp_item.id, "score": 0.90, "source": "vector"},
                {"item_id": chemistry_item.id, "score": 0.89, "source": "vector"},
            ]
        ),
    )

    ranked = _retrieve_then_rank(
        "计算机和化学交叉方向",
        ["NLP", "分析化学"],
        settings=isolated_app_state,
        top_k=5,
    )

    ranked_ids = [candidate["item_id"] for candidate in ranked]
    assert nlp_item.id in ranked_ids
    assert chemistry_item.id in ranked_ids
    by_id = {candidate["item_id"]: candidate for candidate in ranked}
    retained_org_units = {nlp_item.org_unit, chemistry_item.org_unit}
    assert retained_org_units == {"计算机学院", "化学学院"}
    assert by_id[nlp_item.id]["domain"] != "general_unknown"
    assert by_id[chemistry_item.id]["domain"] != "general_unknown"
    assert by_id[nlp_item.id]["graph_evidence"]
    assert by_id[chemistry_item.id]["graph_evidence"]
