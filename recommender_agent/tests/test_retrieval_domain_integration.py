from sqlmodel import Session

from app.models import GraphEdge, Item


class _FakeVectorService:
    def __init__(self, results: list[dict[str, object]]) -> None:
        self.results = results
        self.requested_top_k: int | None = None

    def search_similar(self, query: str, top_k: int = 10) -> list[dict[str, object]]:
        self.requested_top_k = top_k
        return self.results[:top_k]


def _add_item(
    session: Session,
    *,
    title: str,
    org_unit: str,
    research_areas: str,
    tags: str,
    popularity: float = 0.8,
) -> Item:
    item = Item(
        title=title,
        category=org_unit,
        org_unit=org_unit,
        research_areas=research_areas,
        tags=tags,
        description=research_areas,
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


def test_retrieve_candidates_enriches_domain_and_preserves_channel_sources(
    isolated_app_state,
    db_session: Session,
    monkeypatch,
) -> None:
    isolated_app_state.ENABLE_VECTOR = True
    isolated_app_state.ENABLE_GRAPH = True
    isolated_app_state.ENABLE_DOMAIN_GATE = True
    isolated_app_state.ENABLE_RERANKER = True
    isolated_app_state.RERANK_TOP_N = 50
    isolated_app_state.GRAPH_MAX_DEPTH = 2

    nlp_item = _add_item(
        db_session,
        title="张三",
        org_unit="计算机学院",
        research_areas="自然语言处理、知识图谱、推荐系统",
        tags="教授 / 博导",
    )
    chemistry_item = _add_item(
        db_session,
        title="李四",
        org_unit="化学学院",
        research_areas="分析化学、催化",
        tags="教授",
    )
    _add_edge(
        db_session,
        f"Item_{nlp_item.id}",
        "Item",
        "ResearchArea_NLP",
        "ResearchArea",
        "has_research_area",
        evidence="direct NLP profile evidence",
    )
    db_session.commit()

    from app.services.retrieval_service import RetrievalService

    fake_vector = _FakeVectorService(
        [
            {"item_id": nlp_item.id, "score": 0.92, "source": "vector"},
            {"item_id": chemistry_item.id, "score": 0.91, "source": "vector"},
        ]
    )
    monkeypatch.setattr("app.services.retrieval_service.VectorService", lambda: fake_vector)
    service = RetrievalService()

    results = service.retrieve_candidates(
        query="NLP 和知识图谱方向导师推荐",
        keywords=["NLP", "知识图谱"],
        org_unit="计算机学院",
        title="教授",
        top_k=2,
    )

    assert fake_vector.requested_top_k == 50
    assert [candidate["item_id"] for candidate in results] == [nlp_item.id]
    candidate = results[0]
    assert set(candidate["sources"]) == {"vector", "graph", "structured"}
    assert candidate["semantic_score"] == 0.92
    assert candidate["graph_score"] > 0
    assert candidate["graph_evidence"]
    assert candidate["path"] == ["ResearchArea_NLP", f"Item_{nlp_item.id}"]
    assert candidate["relations"] == ["has_research_area"]
    assert candidate["contributing_terms"] == ["NLP"]
    assert candidate["domain"] == "computer_information"
    assert candidate["domain_match"] is True
    assert candidate["domain_score"] > 0
    assert candidate["domain_evidence"]


def test_retrieve_candidates_preserves_empty_domain_gate_fallback(
    isolated_app_state,
    db_session: Session,
    monkeypatch,
) -> None:
    isolated_app_state.ENABLE_DOMAIN_GATE = True
    isolated_app_state.DOMAIN_GATE_MODE = "hard"
    isolated_app_state.DOMAIN_GATE_FALLBACK_ON_EMPTY = True

    item = _add_item(
        db_session,
        title="王五",
        org_unit="历史学院",
        research_areas="中国古代史、文献学",
        tags="教授",
    )

    from app.services.retrieval_service import RetrievalService

    monkeypatch.setattr(
        "app.services.retrieval_service.VectorService",
        lambda: _FakeVectorService([]),
    )
    service = RetrievalService()

    results = service.retrieve_candidates(
        query="化学催化方向导师推荐",
        keywords=["化学", "催化"],
        top_k=5,
    )

    assert [candidate["item_id"] for candidate in results] == [item.id]
    candidate = results[0]
    assert candidate["domain"] == "chemistry"
    assert candidate["domain_match"] is False
    assert candidate["domain_score"] == 0.0
    assert candidate["domain_fallback_reason"] == "all_candidates_filtered_by_domain_gate"
    assert "all_candidates_filtered_by_domain_gate" in candidate["domain_evidence"]
    assert candidate["sources"] == ["structured"]
