import math

from sqlmodel import Session

from app.models import GraphEdge
from app.services.graph_service import GraphService


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


def test_load_graph_preserves_confidence_and_evidence(
    isolated_app_state,
    db_session: Session,
) -> None:
    isolated_app_state.ENABLE_GRAPH = True
    _add_edge(
        db_session,
        "Item_1",
        "Item",
        "ResearchArea_NLP",
        "ResearchArea",
        "has_research_area",
        weight=0.8,
        confidence=0.7,
        evidence="profile text",
    )
    db_session.commit()

    graph_service = GraphService()
    graph_service.load_graph()

    edge_data = graph_service.graph.get_edge_data("Item_1", "ResearchArea_NLP")
    assert edge_data["relation"] == "has_research_area"
    assert edge_data["weight"] == 0.8
    assert edge_data["confidence"] == 0.7
    assert edge_data["evidence"] == "profile text"


def test_recommend_by_graph_diffuses_from_research_area_to_second_hop_items(
    isolated_app_state,
    db_session: Session,
) -> None:
    isolated_app_state.ENABLE_GRAPH = True
    isolated_app_state.GRAPH_MAX_DEPTH = 2
    isolated_app_state.GRAPH_DEPTH_DECAY = 0.5

    _add_edge(
        db_session,
        "Item_1",
        "Item",
        "ResearchArea_NLP",
        "ResearchArea",
        "has_research_area",
        weight=1.0,
        confidence=1.0,
        evidence="direct NLP",
    )
    _add_edge(
        db_session,
        "ResearchArea_NLP",
        "ResearchArea",
        "ResearchArea_KG",
        "ResearchArea",
        "similar_research",
        weight=0.8,
        confidence=0.5,
        evidence="semantic overlap",
    )
    _add_edge(
        db_session,
        "Item_2",
        "Item",
        "ResearchArea_KG",
        "ResearchArea",
        "has_research_area",
        weight=1.0,
        confidence=1.0,
        evidence="second hop KG",
    )
    db_session.commit()

    results = GraphService().recommend_by_graph(["nlp"], top_k=10)

    assert [result["item_id"] for result in results] == [1, 2]
    assert results[0]["score"] == 1.0
    assert 0.0 < results[1]["score"] < results[0]["score"]

    second = results[1]
    assert math.isclose(second["raw_score"], 0.13)
    assert second["graph_evidence"][0]["path"] == [
        "ResearchArea_NLP",
        "ResearchArea_KG",
        "Item_2",
    ]
    assert second["graph_evidence"][0]["relations"] == [
        "similar_research",
        "has_research_area",
    ]
    assert second["graph_evidence"][0]["terms"] == ["nlp"]


def test_recommend_by_graph_traverses_item_similar_research_edges(
    isolated_app_state,
    db_session: Session,
) -> None:
    isolated_app_state.ENABLE_GRAPH = True
    isolated_app_state.GRAPH_MAX_DEPTH = 2
    isolated_app_state.GRAPH_DEPTH_DECAY = 0.5

    _add_edge(
        db_session,
        "Item_1",
        "Item",
        "ResearchArea_NLP",
        "ResearchArea",
        "has_research_area",
        weight=1.0,
        confidence=1.0,
        evidence="direct NLP",
    )
    _add_edge(
        db_session,
        "Item_1",
        "Item",
        "Item_2",
        "Item",
        "similar_research",
        weight=0.8,
        confidence=0.5,
        evidence="NLP,knowledge graph",
    )
    db_session.commit()

    results = GraphService().recommend_by_graph(["nlp"], top_k=10)

    assert [result["item_id"] for result in results] == [1, 2]
    assert math.isclose(results[1]["raw_score"], 0.13)
    assert results[1]["graph_evidence"][0]["path"] == [
        "ResearchArea_NLP",
        "Item_1",
        "Item_2",
    ]
    assert results[1]["graph_evidence"][0]["relations"] == [
        "has_research_area",
        "similar_research",
    ]


def test_recommend_by_graph_uses_tag_matches_but_penalizes_generic_tags(
    isolated_app_state,
    db_session: Session,
) -> None:
    isolated_app_state.ENABLE_GRAPH = True
    isolated_app_state.GRAPH_MAX_DEPTH = 2
    isolated_app_state.GRAPH_DEPTH_DECAY = 0.5

    _add_edge(
        db_session,
        "Item_1",
        "Item",
        "ResearchArea_NLP",
        "ResearchArea",
        "has_research_area",
        weight=1.0,
        confidence=1.0,
    )
    _add_edge(
        db_session,
        "Item_2",
        "Item",
        "Tag_NLP",
        "Tag",
        "has_tag",
        weight=1.0,
        confidence=1.0,
    )
    _add_edge(
        db_session,
        "Item_3",
        "Item",
        "Tag_AI",
        "Tag",
        "has_tag",
        weight=1.0,
        confidence=1.0,
    )
    db_session.commit()

    graph_service = GraphService()
    graph_service.load_graph()
    graph_service.graph.nodes["ResearchArea_NLP"]["label"] = "NLP"
    graph_service.graph.nodes["Tag_NLP"]["label"] = "NLP"
    graph_service.graph.nodes["Tag_AI"]["label"] = "AI"

    results = graph_service.recommend_by_graph(["nlp"], top_k=10)

    assert [result["item_id"] for result in results] == [1, 2]
    assert results[0]["score"] == 1.0
    assert math.isclose(results[1]["raw_score"], 0.85)


def test_from_source_edges_never_contribute_diffusion_score(
    isolated_app_state,
    db_session: Session,
) -> None:
    isolated_app_state.ENABLE_GRAPH = True
    isolated_app_state.GRAPH_MAX_DEPTH = 2
    isolated_app_state.GRAPH_DEPTH_DECAY = 0.5

    _add_edge(
        db_session,
        "SourceSystem_scu",
        "SourceSystem",
        "ResearchArea_NLP",
        "ResearchArea",
        "from_source",
        weight=1.0,
        confidence=1.0,
    )
    _add_edge(
        db_session,
        "SourceSystem_scu",
        "SourceSystem",
        "Item_1",
        "Item",
        "from_source",
        weight=1.0,
        confidence=1.0,
    )
    _add_edge(
        db_session,
        "Item_2",
        "Item",
        "ResearchArea_NLP",
        "ResearchArea",
        "has_research_area",
        weight=1.0,
        confidence=1.0,
    )
    db_session.commit()

    results = GraphService().recommend_by_graph(["nlp"], top_k=10)

    assert [result["item_id"] for result in results] == [2]
    assert results[0]["score"] == 1.0


def test_recommend_by_graph_is_deterministic_and_respects_candidate_limit(
    isolated_app_state,
    db_session: Session,
) -> None:
    isolated_app_state.ENABLE_GRAPH = True
    isolated_app_state.GRAPH_MAX_CANDIDATES = 2

    for item_id in (3, 1, 2):
        _add_edge(
            db_session,
            f"Item_{item_id}",
            "Item",
            "ResearchArea_NLP",
            "ResearchArea",
            "has_research_area",
            weight=1.0,
            confidence=1.0,
        )
    db_session.commit()

    graph_service = GraphService()
    first = graph_service.recommend_by_graph(["nlp"], top_k=10)
    second = graph_service.recommend_by_graph(["nlp"], top_k=10)

    assert first == second
    assert [result["item_id"] for result in first] == [1, 2]
    assert all(0.0 <= result["score"] <= 1.0 for result in first)
