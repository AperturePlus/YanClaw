from __future__ import annotations

import asyncio

import click
import pytest
from click.testing import CliRunner

from agents.crawler import cli as crawler_cli
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


async def test_crawl_async_wraps_import_error_as_click_exception(tmp_path, monkeypatch):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nA,https://a.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    settings = CrawlerSettings(
        websites_path=websites,
        crawler_skills_dir=tmp_path / "skills",
        university_db_dir=tmp_path / "universities",
    )

    async def _raise_import_error(self, universities):
        raise ImportError("playwright is required for PlaywrightFetcher")

    monkeypatch.setattr(CrawlDispatcher, "run", _raise_import_error)

    with pytest.raises(click.ClickException, match="playwright is required for PlaywrightFetcher"):
        await crawler_cli._crawl_async(
            settings,
            universities=["A"],
            skip_llm_check=True,
        )


def test_crawl_cli_auto_installs_chromium_for_playwright_backend(tmp_path, monkeypatch):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nA,https://a.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("YANCLAW_WEBSITES_PATH", str(websites))
    monkeypatch.setenv("YANCLAW_CRAWLER_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("YANCLAW_UNIVERSITY_DB_DIR", str(tmp_path / "universities"))

    called = {"install": 0, "crawl_async": 0}

    def _install():
        called["install"] += 1
        return True

    async def _noop_crawl_async(settings, universities, *, skip_llm_check=False, run_timeout_seconds=None):
        called["crawl_async"] += 1

    monkeypatch.setattr(crawler_cli, "ensure_chromium_installed", _install)
    monkeypatch.setattr(crawler_cli, "_crawl_async", _noop_crawl_async)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "crawl",
        "--fetcher-backend", "playwright",
        "--universities", "A",
        "--skip-llm-check",
    ])

    assert result.exit_code == 0, result.output
    assert called["install"] == 1
    assert called["crawl_async"] == 1


def test_crawl_cli_wraps_chromium_prepare_error(tmp_path, monkeypatch):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nA,https://a.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("YANCLAW_WEBSITES_PATH", str(websites))
    monkeypatch.setenv("YANCLAW_CRAWLER_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("YANCLAW_UNIVERSITY_DB_DIR", str(tmp_path / "universities"))

    def _raise_prepare_error():
        raise RuntimeError("network blocked")

    monkeypatch.setattr(crawler_cli, "ensure_chromium_installed", _raise_prepare_error)

    runner = CliRunner()
    result = runner.invoke(cli, [
        "crawl",
        "--fetcher-backend", "playwright",
        "--universities", "A",
        "--skip-llm-check",
    ])

    assert result.exit_code != 0
    assert "Failed to prepare Playwright Chromium" in result.output


def test_dispatcher_factory_supports_hybrid_backend(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nA,https://a.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    settings = CrawlerSettings(
        websites_path=websites,
        crawler_skills_dir=tmp_path / "skills",
        university_db_dir=tmp_path / "universities",
        fetcher_backend="hybrid",
    )
    factory = CrawlDispatcher._default_fetcher_factory(settings)
    fetcher = factory()
    assert type(fetcher).__name__ == "HybridFetcher"
