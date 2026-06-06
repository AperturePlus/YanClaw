from __future__ import annotations

import pytest

from agents.crawler.sanitizer import (
    contains_self_academician_hint,
    infer_research_areas_from_bio,
    normalize_name,
    normalize_name_key,
    sanitize_professor_payload,
)


def test_sanitize_professor_payload_normalizes_title_and_nullish_fields():
    cleaned, is_academician = sanitize_professor_payload(
        {
            "name": "  Ada  ",
            "title": "Professor (Chair Professor)",
            "email": "",
            "phone": "N/A",
            "enrollment_pref": "PhD Supervisor",
        },
        org_unit_name="Computer Science",
    )

    assert cleaned["name"] == "Ada"
    assert cleaned["org_unit_name"] == "Computer Science"
    assert cleaned["title"] == "教授"
    assert cleaned["email"] is None
    assert cleaned["phone"] is None
    assert cleaned["enrollment_pref"] == "PhD Supervisor"
    assert is_academician is False


def test_sanitize_professor_payload_detects_academician_and_infers_enrollment():
    cleaned, is_academician = sanitize_professor_payload(
        {
            "name": "Someone",
            "title": "教授, 博导, 中国工程院院士",
        },
        org_unit_name="Materials",
    )

    assert is_academician is True
    assert cleaned["title"] == "院士"
    assert cleaned["enrollment_pref"] == "博士生导师"


def test_sanitize_professor_payload_detects_academician_from_flag_and_bio():
    cleaned, is_academician = sanitize_professor_payload(
        {
            "name": "李未",
            "title": "教授",
            "is_academician": True,
            "bio": "李未院士是我国著名的计算机科学家。",
        },
        org_unit_name="计算机学院",
    )

    assert is_academician is True
    assert cleaned["title"] == "院士"


def test_contains_self_academician_hint_accepts_only_self_identity():
    assert contains_self_academician_hint("李未", "李未，中国科学院院士，博士生导师。")
    assert contains_self_academician_hint("李未", "李未院士是我国著名的计算机科学家。")
    assert contains_self_academician_hint("Someone", "教授, 博导, 中国工程院院士")
    assert contains_self_academician_hint("宫本一夫", "宮本一夫 博士，美国艺术与科学院院士（2018）。")

    assert not contains_self_academician_hint("王峰", "博士毕业于北京航空航天大学，导师为张彦仲院士。")
    assert not contains_self_academician_hint(
        "雷文强",
        "与荷兰皇家科学院院士Maarten de Rijke教授等世界一流学者联袂作tutorial。",
    )
    assert not contains_self_academician_hint("汤海波", "王华明院士领衔的重点领域创新团队骨干成员。")
    assert not contains_self_academician_hint("黄晶", "参与国家自然科学基金、院士工作站项目等科研项目。")


def test_sanitize_professor_payload_ignores_unverified_academician_flag():
    cleaned, is_academician = sanitize_professor_payload(
        {
            "name": "雷文强",
            "title": "教授",
            "is_academician": True,
            "bio": "与荷兰皇家科学院院士Maarten de Rijke教授等世界一流学者合作。",
        },
        org_unit_name="计算机学院",
    )

    assert is_academician is False
    assert cleaned["title"] == "教授"


@pytest.mark.parametrize(
    "research_areas",
    [
        "Email",
        "hmy@uestc.edu.cn",
        "Email：hmy@uestc.edu.cn",
    ],
)
def test_sanitize_professor_payload_drops_contact_values_from_research_areas(research_areas):
    cleaned, _ = sanitize_professor_payload(
        {
            "name": "何明耘",
            "title": "教授",
            "research_areas": research_areas,
        },
        org_unit_name="信息与软件工程学院",
    )

    assert cleaned["research_areas"] is None


def test_sanitize_professor_payload_accepts_trusted_snapshot_academician_evidence():
    cleaned, is_academician = sanitize_professor_payload(
        {
            "name": "李未",
            "title": "教授",
            "is_academician": True,
            "_self_academician_evidence": True,
        },
        org_unit_name="计算机学院",
    )

    assert is_academician is True
    assert cleaned["title"] == "院士"


def test_infer_research_areas_from_li_wei_bio():
    bio = (
        "李未院士在实用并发语言操作语义、形式理论序列和修正演算等方面取得了开创性研究成果。"
        "他对科学理论的进化、软件及其功能描述中的缺陷和程序错误的定位和修复进行了系统深入的研究，"
        "提出了关于版本序列的理论，建立了对错误进行修正的形式演算系统（R-演算）；"
        "设计了描述软件开发方法的形式语言，提出了可信软件开发方法的基本理论；"
        "李未院士在我国率先倡导进行海量信息计算的理论与方法研究。"
    )

    inferred = infer_research_areas_from_bio(bio)

    assert inferred is not None
    assert "实用并发语言操作语义" in inferred
    assert "形式理论序列" in inferred
    assert "修正演算" in inferred
    assert "海量信息计算的理论与方法" in inferred


def test_sanitize_professor_payload_does_not_copy_homepage_into_external_link():
    cleaned, _ = sanitize_professor_payload(
        {
            "name": "Ada",
            "homepage": "https://cs.testu.edu.cn/info/1001/1.htm",
        },
        org_unit_name="Computer Science",
    )

    assert cleaned["homepage"] == "https://cs.testu.edu.cn/info/1001/1.htm"
    assert cleaned["external_link"] is None


def test_normalize_name_removes_only_cjk_internal_spaces():
    assert normalize_name("王 俊") == "王俊"
    assert normalize_name("汪　莎") == "汪莎"
    assert normalize_name("李\u200b 四") == "李四"
    assert normalize_name("王俊（兼）") == "王俊"
    assert normalize_name("王俊(兼职)") == "王俊"
    assert normalize_name("王 俊（兼）") == "王俊"
    assert normalize_name("王俊（人工智能）") == "王俊（人工智能）"
    assert normalize_name("Keizo Fujimoto (藤本桂三)") == "Keizo Fujimoto"
    assert normalize_name("Keizo Fujimoto（藤本桂三）") == "Keizo Fujimoto"
    assert normalize_name("John Smith (visiting)") == "John Smith (visiting)"
    assert normalize_name("John Smith") == "John Smith"
    assert normalize_name_key("王 俊") == "王俊"
    assert normalize_name_key("王俊（兼）") == "王俊"
    assert normalize_name_key("Keizo Fujimoto (藤本桂三)") == "Keizo Fujimoto"
    assert normalize_name_key("John Smith") == "John Smith"
