from __future__ import annotations

import pytest

from agents.crawler.agent_detail import _looks_like_profile_detail_url
from agents.crawler.text_repair import repair_mojibake_text
from agents.crawler.professor_noise import should_skip_professor_llm
from agents.crawler.agent import (
    _ExtractionOutcome as LegacyExtractionOutcome,
    _ExtractionTaskItem as LegacyExtractionTaskItem,
    _QueuedUrl as LegacyQueuedUrl,
    _SaveEvent as LegacySaveEvent,
)
from agents.crawler.extraction_models import (
    ExtractionOutcome,
    ExtractionTaskItem,
    QueuedUrl,
    SaveEvent,
)
from agents.crawler.extraction_payloads import ExtractionPayloadService
from agents.crawler.sanitizer import contains_postdoc_hint, normalize_name_key


class _NoopLogger:
    def debug(self, *args, **kwargs) -> None:
        return None

    def info(self, *args, **kwargs) -> None:
        return None


class _PayloadHarness(ExtractionPayloadService):
    def __init__(self) -> None:
        self._pipeline_stats: dict[str, int] = {}
        self.logger = _NoopLogger()
        self._manual_org_unit_aliases_by_name: dict[str, set[str]] = {}

    @staticmethod
    def _normalize_org_unit_match_text(value: str) -> str:
        return str(value or "").strip().lower()


def _task(**overrides) -> ExtractionTaskItem:
    data = {
        "task_id": 1,
        "university": "TestU",
        "org_unit_name": "计算机学院",
        "org_unit_url": "https://cs.scu.edu.cn/",
        "source_url": "https://cs.scu.edu.cn/szdw.htm",
        "page_url": "https://cs.scu.edu.cn/szdw.htm",
        "page_hash": "hash",
        "page_text_snapshot": "",
        "allowed_tools": ["save_professors"],
    }
    data.update(overrides)
    return ExtractionTaskItem(**data)


def test_extraction_models_are_importable_and_keep_legacy_agent_alias():
    queued = QueuedUrl(
        url="https://www.example.edu.cn/faculty",
        depth=2,
        identity_url="https://www.example.edu.cn/faculty?page=1",
    )
    task = _task()
    event = SaveEvent(task=task, payloads=[{"professors": [{"name": "Ada"}]}])
    outcome = ExtractionOutcome(payloads=event.payloads, invalid_json_events=[])

    assert queued.queue_url == "https://www.example.edu.cn/faculty?page=1"
    assert LegacyQueuedUrl is QueuedUrl
    assert LegacyExtractionTaskItem is ExtractionTaskItem
    assert LegacySaveEvent is SaveEvent
    assert LegacyExtractionOutcome is ExtractionOutcome
    assert event.task is task
    assert outcome.payloads == event.payloads


def test_payload_service_suppresses_list_page_professor_payloads():
    harness = _PayloadHarness()
    detail_url = "https://cs.scu.edu.cn/info/1292/17098.htm"
    task = _task(name_homepage_candidates={normalize_name_key("孙元"): detail_url})
    payload = {
        "org_unit_name": "计算机学院",
        "professors": [{"name": "孙元", "title": "副研究员"}],
    }

    normalized = harness._normalize_extraction_payload_for_task(payload, task=task)

    assert normalized is None
    assert harness._pipeline_stats["list_payloads_suppressed"] == 1
    assert harness._pipeline_stats["list_records_suppressed"] == 1


def test_payload_service_drops_detail_name_only_payload():
    harness = _PayloadHarness()
    detail_url = "https://cs.scu.edu.cn/info/1292/17098.htm"
    task = _task(source_url=detail_url, page_url=detail_url, detail_mode=True)

    normalized = harness._normalize_extraction_payload_for_task(
        {
            "org_unit_name": "计算机学院",
            "source_url": detail_url,
            "professors": [{"name": "孙元"}],
        },
        task=task,
    )

    assert normalized is None
    assert harness._pipeline_stats["detail_records_dropped_low_evidence"] == 1
    assert harness._pipeline_stats["detail_payloads_dropped_no_evidence"] == 1


def test_payload_service_suppresses_extra_professors_on_single_detail_page():
    harness = _PayloadHarness()
    detail_url = "https://cs.scu.edu.cn/info/1292/17098.htm"
    task = _task(source_url=detail_url, page_url=detail_url, detail_mode=True)

    normalized = harness._normalize_extraction_payload_for_task(
        {
            "org_unit_name": "计算机学院",
            "source_url": detail_url,
            "professors": [
                {"name": "弱证据", "title": "讲师"},
                {
                    "name": "强证据",
                    "title": "副研究员",
                    "email": "strong@scu.edu.cn",
                    "research_areas": "多模态智能",
                },
            ],
        },
        task=task,
    )

    assert normalized is not None
    assert [item["name"] for item in normalized["professors"]] == ["强证据"]
    assert harness._pipeline_stats["detail_multi_professor_suppressed"] == 1


def test_payload_service_synthesizes_payload_from_detail_snapshot():
    harness = _PayloadHarness()
    detail_url = "https://cs.scu.edu.cn/info/1292/17098.htm"
    task = _task(
        source_url=detail_url,
        page_url=detail_url,
        detail_mode=True,
        page_text_snapshot=(
            "## 孙元\n"
            "职称：副研究员\n"
            "电子邮箱：sunyuan@scu.edu.cn\n"
            "研究方向：多模态智能、AI for CFD\n"
            "个人简介：孙元，四川大学计算机学院教师，主要研究多模态学习、图像融合、"
            "智能科学计算和物理信息人工智能。近年来发表人工智能领域论文多篇，"
            "长期承担科研项目并指导学生开展相关研究。\n"
        ),
    )
    payloads: list[dict] = []

    assert harness._apply_detail_snapshot_profile_fallback(payloads, task) is True

    professor = payloads[0]["professors"][0]
    assert payloads[0]["org_unit_name"] == "计算机学院"
    assert professor["name"] == "孙元"
    assert professor["homepage"] == detail_url
    assert professor["email"] == "sunyuan@scu.edu.cn"
    assert harness._pipeline_stats["detail_snapshot_payloads_synthesized"] == 1


def test_payload_service_infers_academician_flag_from_detail_context():
    harness = _PayloadHarness()
    task = _task(
        detail_mode=True,
        page_text_snapshot=(
            "## 侯朝焕\n"
            "侯朝焕，于1995年当选中国科学院院士，现任中国科学院声学所研究员、"
            "博士生导师，长期从事声学信号处理和信息处理研究。"
        ),
    )
    payload = {"professors": [{"name": "侯朝焕", "title": "教授"}]}

    harness._infer_academician_flags_from_detail_context(payload, task=task)

    assert payload["professors"][0]["is_academician"] is True
    assert payload["professors"][0]["_self_academician_evidence"] is True
    assert harness._pipeline_stats["academician_flags_inferred_from_detail"] == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://www.cs.sjtu.edu.cn/jiaoshiml/duanshengxiong.html",
        "https://www.cs.sjtu.edu.cn/jiaoshiml/zhangzhuosheng.html",
        "https://example.edu.cn/teacher/lisiming.html",
        "https://example.edu.cn/info/1001/2002.htm",  # existing pattern still works
    ],
)
def test_faculty_section_profile_url_is_detail(url):
    assert _looks_like_profile_detail_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.cs.sjtu.edu.cn/jiaoshiml.html",   # the roster itself, not a profile
        "https://www.cs.sjtu.edu.cn/jiaoshiml/index.html",
        "https://www.cs.sjtu.edu.cn/jiaoshiml/list.html",
        "https://www.cs.sjtu.edu.cn/jiaoshiml/123.html",  # numeric = pagination/category
        "https://www.cs.sjtu.edu.cn/szdw.html",        # section landing, no name leaf
        "https://www.cs.sjtu.edu.cn/xygk.html",        # unrelated section
        "https://cs.scu.edu.cn/szdw/jczx.htm",         # SCU sub-department section (container dir)
        "https://cs.scu.edu.cn/szdw/lisiming.htm",     # name-shaped leaf under a container dir stays a section
        "https://dept3.buaa.edu.cn/szjs/zzjs/jcyzdhgcx.htm",  # BUAA sub-roster, not a person
    ],
)
def test_non_profile_faculty_urls_are_not_detail(url):
    assert _looks_like_profile_detail_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        # SJTU AI school publishes faculty at extensionless `…/facultydetails/<section>/<slug>`.
        "https://soai.sjtu.edu.cn/cn/facultydetails/zzjs/zhanglinfeng",
        "https://soai.sjtu.edu.cn/cn/facultydetails/zzjs/caoqinxiang",
        # The "detail" marker also works with an extension and other section/detail dirs.
        "https://example.edu.cn/cn/teacherdetails/js/lisiming.html",
        "https://example.edu.cn/szdwdetails/szdw/wangwu",
    ],
)
def test_faculty_detail_marker_profile_url_is_detail(url):
    assert _looks_like_profile_detail_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://soai.sjtu.edu.cn/cn/faculty/zzjs",               # the roster (no detail marker)
        "https://soai.sjtu.edu.cn/cn/facultydetails/zzjs",        # the category dir, no person leaf
        "https://soai.sjtu.edu.cn/cn/facultydetails/zzjs/index",  # landing leaf
        "https://soai.sjtu.edu.cn/cn/facultydetails/zzjs/123",    # numeric leaf
    ],
)
def test_faculty_detail_marker_non_profiles_are_not_detail(url):
    assert _looks_like_profile_detail_url(url) is False


@pytest.mark.parametrize(
    "url,text",
    [
        # Breadcrumb trail's bolded terminal is 博士后, even though the site nav lists a
        # (non-bold) 博士后 link on every page.
        (
            "https://soai.sjtu.edu.cn/cn/show/433",
            "[ 师资队伍 ](/cn/faculty/zzjs)\n[ 专职教师 __](/cn/faculty/zzjs)\n"
            "[ 博士后 __](/cn/teacher/bsh)\n\n"
            "[__首页](/) _/_ [师资队伍](/cn/faculty/zzjs) _/_[**博士后**](/cn/teacher/bsh)\n\n"
            "# 张赟\n张赟博士后，2025年毕业于上海交通大学。邮箱：zhang_yun@sjtu.edu.cn",
        ),
        # Postdoc section is encoded in the URL path itself.
        (
            "https://x.edu.cn/cn/teacher/bsh/wanghaiwen",
            "王海文 教授 邮箱 wang@x.edu.cn 研究方向 人工智能",
        ),
    ],
)
def test_professor_gate_skips_postdoc_section(url, text):
    skip, reason = should_skip_professor_llm(url=url, text=text)
    assert skip is True
    assert reason == "postdoc_section"


def test_professor_gate_keeps_full_time_faculty_breadcrumb():
    # Same nav (with its ubiquitous 博士后 link), but the breadcrumb terminal is 专职教师,
    # so the page is a real-faculty profile and must NOT be skipped.
    skip, reason = should_skip_professor_llm(
        url="https://soai.sjtu.edu.cn/cn/facultydetails/zzjs/zhanglinfeng",
        text=(
            "[ 师资队伍 ](/cn/faculty/zzjs)\n[ 专职教师 __](/cn/faculty/zzjs)\n"
            "[ 博士后 __](/cn/teacher/bsh)\n\n"
            "[__首页](/) _/_ [师资队伍](/cn/faculty/zzjs) _/_[**专职教师**](/cn/faculty/zzjs)\n\n"
            "张林峰\n职称：助理教授\n邮箱：zhanglinfeng@sjtu.edu.cn\n研究方向：模型压缩"
        ),
    )
    assert skip is False
    assert reason == ""


@pytest.mark.parametrize(
    "name,title,bio,source_url",
    [
        ("李四", "博士后", "", "https://x.edu.cn/show/1"),               # explicit postdoc title
        ("陈某", "师资博士后", "", "https://x.edu.cn/show/2"),            # faculty-track postdoc, still excluded
        ("张赟", "教授", "张赟博士后，2025年毕业于上海交通大学，合作导师为严骏驰教授。",
         "https://soai.sjtu.edu.cn/cn/show/433"),                       # bio opens "X博士后…"; advisor-mislabeled title
        ("王海文", "", "", "https://soai.sjtu.edu.cn/cn/teacher/bsh/wanghaiwen"),  # postdoc section in URL
    ],
)
def test_contains_postdoc_hint_flags_postdocs(name, title, bio, source_url):
    assert contains_postdoc_hint(name=name, title=title, bio=bio, source_url=source_url) is True


@pytest.mark.parametrize(
    "name,title,bio,source_url",
    [
        # Real professor who WON a postdoc-named funding program — must NOT be filtered.
        ("孙元", "教授", '孙元，入选四川大学"海纳博士后"资助计划（15名），主要研究方向为多模态智能。',
         "https://cs.scu.edu.cn/szdw/sunyuan.htm"),
        # Career-history mention ("从事博士后研究") — kept.
        ("张三", "教授", "张三，2015年至2017年在清华大学从事博士后研究，现为教授。",
         "https://x.edu.cn/teacher/zhangsan"),
        # Ordinary professor — kept.
        ("李教授", "副教授", "主要研究方向为机器学习。", "https://x.edu.cn/faculty/li"),
    ],
)
def test_contains_postdoc_hint_keeps_real_professors(name, title, bio, source_url):
    assert contains_postdoc_hint(name=name, title=title, bio=bio, source_url=source_url) is False


def test_repair_mojibake_recovers_utf8_misread_as_latin1():
    # Captured corruption: a correctly-decoded UTF-8 page's bytes get reinterpreted
    # as Latin-1 at the GM_xmlhttpRequest transport boundary. Always reversible.
    original = "人才招聘 段圣雄 教授"
    broken = original.encode("utf-8").decode("latin-1")
    assert broken != original
    assert repair_mojibake_text(broken) == original


def test_repair_mojibake_leaves_clean_text_untouched():
    clean = "段圣雄 教授 Professor email duan@cs.sjtu.edu.cn"
    assert repair_mojibake_text(clean) == clean


def test_repair_mojibake_leaves_pure_ascii_untouched():
    clean = "Ada Professor email ada@example.edu.cn"
    assert repair_mojibake_text(clean) == clean


def test_repair_mojibake_leaves_latin_accents_untouched():
    # A genuine Latin-accented name with no CJK must not be mangled into CJK.
    clean = "José Müller"
    assert repair_mojibake_text(clean) == clean
