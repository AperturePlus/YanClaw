from __future__ import annotations

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
from agents.crawler.sanitizer import normalize_name_key


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
