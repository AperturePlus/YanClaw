from __future__ import annotations

import asyncio

from click.testing import CliRunner

from agents.crawler.cli import cli
from agents.crawler.config import CrawlerSettings
from agents.crawler.dispatcher import CrawlDispatcher
from agents.crawler.models import CrawlStatus


class FakeAgent:
    active = 0
    max_active = 0

    def __init__(self, **kwargs):
        self.university_name = kwargs["university_name"]

    async def run(self):
        FakeAgent.active += 1
        FakeAgent.max_active = max(FakeAgent.max_active, FakeAgent.active)
        await asyncio.sleep(0.01)
        FakeAgent.active -= 1
        return type(
            "Result",
            (),
            {
                "university_name": self.university_name,
                "status": CrawlStatus.COMPLETED.value,
                "visited_count": 1,
                "saved_professors": 0,
                "messages": [],
            },
        )()


async def test_dispatcher_filters_and_limits_concurrency(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nA,https://a.example.edu.cn/,X\nB,https://b.example.edu.cn/,Y\n",
        encoding="utf-8",
    )
    settings = CrawlerSettings(
        websites_path=websites,
        crawler_skills_dir=tmp_path / "skills",
        university_db_dir=tmp_path / "universities",
        max_concurrency=1,
        request_interval_seconds=0,
        max_retries=0,
    )
    FakeAgent.active = 0
    FakeAgent.max_active = 0

    dispatcher = CrawlDispatcher(settings=settings, agent_factory=FakeAgent)
    summary = await dispatcher.run(universities=["A", "B"])

    assert summary.success == 2
    assert FakeAgent.max_active == 1


class SlowAgent:
    def __init__(self, **kwargs):
        self.university_name = kwargs["university_name"]

    async def run(self):
        await asyncio.sleep(2)
        return type(
            "Result",
            (),
            {
                "university_name": self.university_name,
                "status": CrawlStatus.COMPLETED.value,
                "visited_count": 1,
                "saved_professors": 0,
                "messages": [],
            },
        )()


async def test_dispatcher_enforces_university_timeout(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nA,https://a.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    settings = CrawlerSettings(
        websites_path=websites,
        crawler_skills_dir=tmp_path / "skills",
        university_db_dir=tmp_path / "universities",
        max_concurrency=1,
        request_interval_seconds=0,
        max_retries=0,
        university_timeout_seconds=0.5,
    )
    dispatcher = CrawlDispatcher(settings=settings, agent_factory=SlowAgent)
    summary = await dispatcher.run(universities=["A"])

    assert summary.failed == 1
    assert summary.results[0].status == CrawlStatus.FAILED.value
    assert any("Timeout after" in message for message in summary.results[0].messages)


def test_university_db_path_differs_per_school(tmp_path):
    from agents.crawler.dispatcher import _university_db_path

    path_a = _university_db_path(tmp_path, "https://www.pku.edu.cn/")
    path_b = _university_db_path(tmp_path, "https://www.tsinghua.edu.cn/")

    assert path_a != path_b
    assert path_a.parent == tmp_path
    assert path_b.parent == tmp_path
    assert path_a.name == "pku.edu.cn.db"
    assert path_b.name == "tsinghua.edu.cn.db"


def test_cli_help_outputs_commands():
    runner = CliRunner()
    result = runner.invoke(cli, ["crawl", "--help"])
    assert result.exit_code == 0
    assert "--universities" in result.output

    result = runner.invoke(cli, ["skills", "--help"])
    assert result.exit_code == 0
    assert "rollback" in result.output

    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "llm-check" in result.output
