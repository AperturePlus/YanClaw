from __future__ import annotations

import json
from pathlib import Path

from agents.crawler.form_pagination import extract_form_pagination_states


FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "crawler" / "dynamic_pagination"
UESTC_URL = (
    "https://faculty.uestc.edu.cn/xylb.jsp?"
    "id=2031&lang=zh_CN&urltype=tsites.CollegeTeacherList&wbtreeid=1021"
)


def test_uestc_fromwen_expands_absolute_page_states():
    html = (FIXTURE_DIR / "uestc_fromwen_page1.html").read_text(encoding="utf-8")
    expected = json.loads((FIXTURE_DIR / "uestc_fromwen_expected.json").read_text(encoding="utf-8"))

    states = extract_form_pagination_states(html, UESTC_URL)

    assert [state.page_index for state in states] == expected["pages"]
    assert {state.form_name for state in states} == {expected["form_name"]}
    assert {next(iter(state.fields)) for state in states} == {expected["page_field"]}
    assert all("NextPage" not in state.state_id for state in states)


def test_uestc_letter_filter_keeps_current_url_context():
    html = (FIXTURE_DIR / "uestc_fromwen_letter_page1.html").read_text(encoding="utf-8")
    url = (
        "https://faculty.uestc.edu.cn/pyjslb.jsp?"
        "lang=zh_CN&py=a&urltype=tsites.PinYinTeacherList&wbtreeid=1021"
    )

    states = extract_form_pagination_states(html, url)

    assert [state.page_index for state in states] == [2, 3]
    assert all("py=a" in state.synthetic_url for state in states)
