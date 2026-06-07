import json
from datetime import datetime

from app.models.item import Item
from app.models.org_unit import OrgUnit
from app.services.evidence_assembler import assemble_recommendation_evidence, match_level
from app.services.recommendation_service import (
    _generate_evidence_reasons,
    _generate_follow_up_questions,
)


def test_assemble_recommendation_evidence_with_complete_metadata():
    item = Item(
        id=1,
        title="张三",
        category="计算机学院",
        description="长期从事自然语言处理研究。",
        research_areas="自然语言处理、知识图谱",
        org_unit="计算机学院",
        tags="教授",
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
    org_unit = OrgUnit(name="计算机学院", url="https://cs.example.edu.cn")

    payload = assemble_recommendation_evidence(
        item=item,
        org_unit=org_unit,
        match_score=0.87654,
        ranking_evidence={"semantic_score": 0.9},
        retrieval_sources=["vector", "structured"],
        reasons=["研究方向与查询匹配"],
    )

    assert payload["name"] == "张三"
    assert payload["university"] == "四川大学"
    assert payload["college"] == "计算机学院"
    assert payload["title"] == "教授"
    assert payload["research_fields"] == ["自然语言处理", "知识图谱"]
    assert payload["summary"] == "长期从事自然语言处理研究。"
    assert payload["homepage_url"] == "https://cs.example.edu.cn/zhangsan"
    assert payload["source_url"] == "https://cs.example.edu.cn/faculty/zhangsan"
    assert payload["updated_at"] == "2026-01-02"
    assert payload["match_score"] == 0.8765
    assert payload["match_level"] == "高"
    assert payload["match_level_en"] == "high"
    assert payload["reasons"] == ["研究方向与查询匹配"]
    assert payload["limitations"] == []
    assert payload["evidence"]["semantic_score"] == 0.9
    assert payload["evidence"]["data_source"] == "scu.edu.cn.db"
    assert payload["evidence"]["imported_at"] == "2026-01-03T04:05:06"


def test_assemble_recommendation_evidence_missing_metadata_has_limitations():
    item = Item(
        id=2,
        title="李四",
        category="",
        description=None,
        research_areas="",
        org_unit=None,
        tags=None,
        metadata_json=json.dumps({"source": "scu.edu.cn.db"}, ensure_ascii=False),
        created_at=datetime(2026, 2, 3, 4, 5, 6),
    )

    payload = assemble_recommendation_evidence(item=item, match_score=0.42)

    assert payload["name"] == "李四"
    assert payload["university"] is None
    assert payload["college"] is None
    assert payload["title"] is None
    assert payload["research_fields"] == []
    assert payload["summary"] is None
    assert payload["homepage_url"] is None
    assert payload["source_url"] is None
    assert payload["updated_at"] is None
    assert payload["match_score"] == 0.42
    assert payload["match_level"] == "低"
    assert payload["match_level_en"] == "low"
    assert payload["follow_up_questions"] == []
    assert "该导师暂无公开主页信息" in payload["limitations"]
    assert "数据来源未标注" in payload["limitations"]
    assert "信息更新时间未记录" in payload["limitations"]
    assert "该导师研究方向未明确标注" in payload["limitations"]
    assert "该导师暂无简介信息" in payload["limitations"]
    assert payload["evidence"]["data_source"] == "scu.edu.cn.db"
    assert payload["evidence"]["imported_at"] == "2026-02-03T04:05:06"
    assert any("匹配分数较低" in limitation for limitation in payload["limitations"])


def test_assemble_recommendation_evidence_does_not_fabricate_bad_metadata():
    item = Item(
        title="王五",
        category="生命科学学院",
        metadata_json="not-json",
        created_at=datetime(2026, 3, 4, 5, 6, 7),
    )

    payload = assemble_recommendation_evidence(item=item)

    assert payload["homepage_url"] is None
    assert payload["source_url"] is None
    assert payload["updated_at"] is None
    assert payload["evidence"]["data_source"] is None
    assert payload["evidence"]["metadata_keys"] == []


def test_match_level_thresholds_support_chinese_and_english_labels():
    assert match_level(0.8) == "高"
    assert match_level(0.7999) == "中"
    assert match_level(0.5) == "中"
    assert match_level(0.4999) == "低"
    assert match_level(0.8, locale="en") == "high"
    assert match_level(0.5, locale="en") == "medium"
    assert match_level(0.49, locale="en") == "low"


def test_generate_evidence_reasons_uses_item_fields_and_scores_not_llm_text():
    item = Item(
        id=3,
        title="赵六",
        research_areas="自然语言处理、知识图谱",
        org_unit="计算机学院",
        tags="教授/博导",
    )
    intent = {
        "query": "计算机学院自然语言处理方向教授",
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
        },
    }

    reasons = _generate_evidence_reasons(
        item,
        intent,
        {"semantic_score": 0.91, "graph_score": 0.42},
    )

    assert len(reasons) >= 4
    assert any("研究方向包含自然语言处理" in reason for reason in reasons)
    assert "所在学院为计算机学院，符合查询中的学院偏好" in reasons
    assert "职称教授/博导，匹配用户的导师类型偏好" in reasons
    assert "通过语义检索（semantic_score: 0.9100）和图关系（graph_score: 0.4200）综合匹配" in reasons


def test_generate_follow_up_questions_for_vague_query_understanding():
    intent = {
        "intent": "general_recommendation",
        "query_understanding": {
            "research_interests": [],
            "preferred_locations": [],
            "preferred_universities": [],
            "degree_stage": "",
            "constraints": [],
            "soft_preferences": [],
            "exclusions": [],
            "uncertainties": ["用户需求较宽泛"],
        },
    }

    assert _generate_follow_up_questions(intent) == [
        "你更倾向于理论研究还是应用研究？",
        "是否只考虑 985 高校？",
        "你计划申请硕士还是博士？",
        "你对地区有偏好吗？",
    ]
