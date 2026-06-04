from __future__ import annotations

from agents.crawler.sanitizer import normalize_name, normalize_name_key, sanitize_professor_payload


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
