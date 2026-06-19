from __future__ import annotations

from pathlib import Path

import pytest

from agents.crawler.agent_detail import extract_detail_profile_record_from_snapshot
from agents.crawler.sanitizer import sanitize_professor_payload
from agents.crawler.url_heuristics import (
    FACULTY_PAGE_TYPE_NOISE,
    _assess_faculty_candidate,
    _is_non_faculty_noise_url,
)


FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "crawler" / "service_guide"


def _fixture_text(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


def test_uestc_service_guide_url_is_non_faculty_noise():
    assert _is_non_faculty_noise_url("https://www.scse.uestc.edu.cn/szdw/bszn.htm")
    assert _is_non_faculty_noise_url("https://www.scse.uestc.edu.cn/szdw/bszn/4.htm")


def test_uestc_service_guide_candidate_is_noise():
    item = _assess_faculty_candidate(
        "https://www.scse.uestc.edu.cn/szdw/bszn.htm",
        anchor_text="办事指南",
        heading_text="师资队伍",
    )

    assert item.page_type == FACULTY_PAGE_TYPE_NOISE
    assert item.score <= 0


def test_service_guide_snapshot_does_not_synthesize_professor_record():
    text = _fixture_text("uestc_service_guide.md")

    record = extract_detail_profile_record_from_snapshot(
        text,
        page_url="https://www.scse.uestc.edu.cn/info/1036/10161.htm",
    )

    assert record is None


def test_sanitizer_rejects_service_guide_as_professor_name():
    with pytest.raises(ValueError, match="Invalid professor name"):
        sanitize_professor_payload({"name": "办事指南"}, org_unit_name="计算机科学与工程学院")
