from __future__ import annotations

from agents.crawler.sanitizer import sanitize_professor_payload


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
