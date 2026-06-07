from typing import cast

from sqlmodel import Session

from app.models.item import Item
from app.core.config import Settings
from app.services.domain_gate import Candidate, DomainGateService, DomainInference, UNKNOWN_DOMAIN


def test_infer_domain_maps_computer_information_terms() -> None:
    service = DomainGateService()

    inference = service.infer_domain(
        "推荐自然语言处理和知识图谱方向导师",
        intent_keywords=["NLP", "机器学习", "深度学习"],
    )

    assert inference.domain == "computer_information"
    assert inference.confidence >= 0.3
    assert any("自然语言处理" in evidence for evidence in inference.evidence)


def test_infer_domain_maps_chemistry_terms() -> None:
    service = DomainGateService()

    inference = service.infer_domain("分析化学和材料化学方向")

    assert inference.domain == "chemistry"
    assert inference.confidence >= 0.3
    assert any("分析化学" in evidence for evidence in inference.evidence)


def test_unknown_domain_scores_candidates_without_filtering() -> None:
    service = DomainGateService()
    candidates: list[Candidate] = [
        {"item_id": 1, "title": "张教授", "research_areas": "分析化学"},
        {"item_id": 2, "title": "李教授", "research_areas": "自然语言处理"},
    ]

    result = service.score_candidates(
        candidates,
        DomainInference(domain=UNKNOWN_DOMAIN, confidence=0.0),
    )

    assert result.domain == UNKNOWN_DOMAIN
    assert result.domain_confidence == 0.0
    assert len(result.candidates) == 2
    assert result.candidates[0]["domain_score"] == 0.0
    assert result.candidates[0]["domain_match"] is False
    assert result.candidates[0]["domain_evidence"] == []


def test_score_candidates_enriches_matches_from_candidate_fields() -> None:
    service = DomainGateService()
    query_domain = service.infer_domain("NLP 导师")
    candidates: list[Candidate] = [
        {
            "item_id": 1,
            "title": "王老师",
            "research_areas": "自然语言处理、知识图谱",
            "org_unit": "计算机学院",
            "tags": "教授",
            "summary": "研究机器学习和深度学习",
        },
        {
            "item_id": 2,
            "title": "赵老师",
            "research_areas": "分析化学",
            "org_unit": "化学学院",
        },
    ]

    result = service.score_candidates(candidates, query_domain)

    assert len(result.candidates) == 2
    assert result.candidates[0]["domain_match"] is True
    first_score = cast(float, result.candidates[0]["domain_score"])
    second_score = cast(float, result.candidates[1]["domain_score"])
    assert first_score > second_score
    assert "domain_evidence" in result.candidates[0]
    first_evidence = cast(list[str], result.candidates[0]["domain_evidence"])
    assert any("自然语言处理" in evidence for evidence in first_evidence)


def test_score_candidates_can_lookup_item_fields_from_session(db_session: Session) -> None:
    item = Item(
        title="陈教授",
        research_areas="医学影像、医学图像分析",
        org_unit="生物医学工程学院",
        tags="教授",
        description="MRI 和 CT 图像重建",
        is_active=True,
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    service = DomainGateService()
    query_domain = service.infer_domain("医学影像导师")
    result = service.score_candidates([{"item_id": item.id}], query_domain, session=db_session)

    assert result.domain == "biomedical_imaging"
    assert result.candidates[0]["domain_match"] is True
    domain_score = cast(float, result.candidates[0]["domain_score"])
    assert domain_score > 0


def test_hard_mode_falls_back_when_all_candidates_filtered() -> None:
    service = DomainGateService(mode="hard", fallback_on_empty=True)
    query_domain = service.infer_domain("航空航天机器人")
    candidates: list[Candidate] = [
        {"item_id": 1, "title": "化学导师", "research_areas": "分析化学", "org_unit": "化学学院"},
        {"item_id": 2, "title": "材料导师", "research_areas": "材料化学", "org_unit": "化学学院"},
    ]

    result = service.score_candidates(candidates, query_domain)

    assert len(result.candidates) == 2
    assert result.fallback_reason == "all_candidates_filtered_by_domain_gate"
    assert all(
        candidate["domain_fallback_reason"] == "all_candidates_filtered_by_domain_gate"
        for candidate in result.candidates
    )


def test_domain_gate_defaults_are_available_from_settings(test_settings: Settings) -> None:
    assert test_settings.ENABLE_DOMAIN_GATE is True
    assert test_settings.DOMAIN_GATE_MODE == "soft"
    assert test_settings.DOMAIN_GATE_MIN_CONFIDENCE == 0.3
    assert test_settings.DOMAIN_GATE_FALLBACK_ON_EMPTY is True
