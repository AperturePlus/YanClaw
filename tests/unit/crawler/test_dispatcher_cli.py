from __future__ import annotations

import asyncio

from click.testing import CliRunner

from agents.crawler.cli import cli
from agents.crawler.config import CrawlerSettings
from agents.crawler.dispatcher import CrawlDispatcher
from agents.crawler.models import CrawlStatus
from runtime.database import DatabaseManager
from tests.conftest import sqlite_url


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
        database_url=sqlite_url(tmp_path / "dispatcher.db"),
        websites_path=websites,
        crawler_skills_dir=tmp_path / "skills",
        max_concurrency=1,
        request_interval_seconds=0,
    )
    db = DatabaseManager(settings.database_url)
    FakeAgent.active = 0
    FakeAgent.max_active = 0

    dispatcher = CrawlDispatcher(settings=settings, db=db, agent_factory=FakeAgent)
    summary = await dispatcher.run(universities=["A", "B"])

    assert summary.success == 2
    assert FakeAgent.max_active == 1
    await db.close()


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
