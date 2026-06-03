from __future__ import annotations

import asyncio
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler import cli as crawler_cli
from agents.crawler import dispatcher as dispatcher_module
from agents.crawler.cli import cli
from agents.crawler.config import CrawlerSettings
from agents.crawler.dispatcher import CrawlDispatcher, FreshRunPreparationError, _university_db_path
from agents.crawler.models import CrawlStatus, OrgUnit, Professor, ProfessorAffiliation
from runtime.database import DatabaseManager
from runtime.skills import SkillMeta
from runtime.cli import cli as runtime_cli


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


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


class NoopFetcher:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


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
    )
    FakeAgent.active = 0
    FakeAgent.max_active = 0

    dispatcher = CrawlDispatcher(settings=settings, agent_factory=FakeAgent, fetcher_factory=NoopFetcher)
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


class CaptureAgent:
    last_kwargs: dict[str, object] | None = None

    def __init__(self, **kwargs):
        CaptureAgent.last_kwargs = kwargs
        self.university_name = kwargs["university_name"]

    async def run(self):
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


def test_crawler_settings_removed_human_only_fake_knobs():
    settings = CrawlerSettings()
    removed = {
        "fetcher_backend",
        "detail_fetch_backend",
        "request_interval_seconds",
        "request_timeout_seconds",
        "max_retries",
        "pipeline_fetch_workers",
        "detail_failure_threshold",
    }
    assert removed.isdisjoint(settings.model_fields_set)
    for name in removed:
        assert not hasattr(settings, name)


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
        university_timeout_seconds=0.5,
    )
    dispatcher = CrawlDispatcher(settings=settings, agent_factory=SlowAgent, fetcher_factory=NoopFetcher)
    summary = await dispatcher.run(universities=["A"])

    assert summary.failed == 1
    assert summary.results[0].status == CrawlStatus.FAILED.value
    assert any("Timeout after" in message for message in summary.results[0].messages)


async def test_dispatcher_passes_org_unit_target_settings_to_agent(tmp_path):
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
        target_org_units=["计算机学院", "软件学院"],
        org_unit_match_threshold=0.7,
    )
    CaptureAgent.last_kwargs = None
    dispatcher = CrawlDispatcher(settings=settings, agent_factory=CaptureAgent, fetcher_factory=NoopFetcher)
    summary = await dispatcher.run(universities=["A"])

    assert summary.success == 1
    assert CaptureAgent.last_kwargs is not None
    assert CaptureAgent.last_kwargs.get("target_org_units") == ["计算机学院", "软件学院"]
    assert CaptureAgent.last_kwargs.get("org_unit_match_threshold") == 0.7


async def test_dispatcher_passes_resume_mode_to_agent(tmp_path):
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
    )
    CaptureAgent.last_kwargs = None
    dispatcher = CrawlDispatcher(settings=settings, agent_factory=CaptureAgent, fetcher_factory=NoopFetcher)
    summary = await dispatcher.run(universities=["A"], resume=True)

    assert summary.success == 1
    assert CaptureAgent.last_kwargs is not None
    assert CaptureAgent.last_kwargs.get("resume_mode") is True


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
    assert "--org-units" in result.output
    assert "--org-unit-match-threshold" in result.output
    assert "--resume" in result.output

    result = runner.invoke(cli, ["skills", "--help"])
    assert result.exit_code == 0
    assert "rollback" in result.output

    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "llm-check" in result.output
    assert "steward" in result.output


async def test_skills_list_outputs_scope_metadata(monkeypatch, tmp_path, capsys):
    class _FakeDb:
        async def close(self):
            return None

    class _FakeManager:
        def list_skills(self):
            return [
                SkillMeta(
                    name="extract-links",
                    description="links",
                    version=3,
                    path=tmp_path / "extract-links.md",
                    applies_to=("FIND_FACULTY_PAGES",),
                    allowed_tools=("extract_links",),
                    priority=10,
                    token_budget=500,
                )
            ]

    async def _fake_manager():
        return _FakeDb(), _FakeManager()

    monkeypatch.setattr(crawler_cli, "_manager", _fake_manager)

    await crawler_cli._list_skills_async()
    output = capsys.readouterr().out
    assert "priority=10" in output
    assert "applies_to=FIND_FACULTY_PAGES" in output
    assert "allowed_tools=extract_links" in output
    assert "token_budget=500" in output


def test_runtime_cli_exposes_crawler_commands():
    runner = CliRunner()
    result = runner.invoke(runtime_cli, ["--help"])
    assert result.exit_code == 0
    assert "crawl" in result.output
    assert "steward" in result.output


def test_crawl_cli_passes_org_unit_filters(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_crawl_async(settings, universities, **kwargs):
        captured["settings_target_org_units"] = list(settings.target_org_units)
        captured["settings_org_unit_match_threshold"] = float(settings.org_unit_match_threshold)
        captured["universities"] = universities
        captured["kwargs"] = kwargs

    monkeypatch.setattr(crawler_cli, "_crawl_async", _fake_crawl_async)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "crawl",
            "--universities",
            "A",
            "--org-units",
            "计算机学院,软件学院",
            "--org-unit-match-threshold",
            "0.7",
            "--skip-llm-check",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["universities"] == ["A"]
    assert captured["settings_target_org_units"] == ["计算机学院", "软件学院"]
    assert captured["settings_org_unit_match_threshold"] == 0.7


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

    async def _raise_import_error(self, universities, *, resume=False):
        raise ImportError("fetcher dependency missing")

    monkeypatch.setattr(CrawlDispatcher, "run", _raise_import_error)

    with pytest.raises(click.ClickException, match="fetcher dependency missing"):
        await crawler_cli._crawl_async(
            settings,
            universities=["A"],
            skip_llm_check=True,
        )


async def test_crawl_async_wraps_fresh_prepare_error_as_click_exception(tmp_path, monkeypatch):
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

    async def _raise_prepare_error(self, universities, *, resume=False):
        raise FreshRunPreparationError("backup failed")

    monkeypatch.setattr(CrawlDispatcher, "run", _raise_prepare_error)

    with pytest.raises(click.ClickException, match="backup failed"):
        await crawler_cli._crawl_async(
            settings,
            universities=["A"],
            skip_llm_check=True,
        )

def test_steward_run_cli_passes_university_selectors(monkeypatch):
    captured: dict[str, object] = {}

    async def _fake_steward_run_async(
        *,
        settings,
        universities,
        universities_file,
        db_roots,
        apply,
        llm_enabled,
        max_context_tokens,
        include_backup_audit,
    ):
        captured["universities"] = universities
        captured["db_roots"] = db_roots
        captured["apply"] = apply
        return type(
            "Summary",
            (),
            {
                "mode": "dry_run",
                "targets": ["x.db"],
                "total_duplicates_detected": 1,
                "total_duplicates_deleted": 0,
                "total_missing_field_audits": 2,
                "total_recrawl_tasks_upserted": 0,
                "unmatched_universities": [],
                "unmatched_db_roots": [],
                "runs": [],
            },
        )()

    monkeypatch.setattr(crawler_cli, "_steward_run_async", _fake_steward_run_async)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "steward",
            "run",
            "--universities",
            "A,B",
            "--db-roots",
            "pku.edu.cn,tsinghua.edu.cn",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["universities"] == ["A", "B"]
    assert captured["db_roots"] == ["pku.edu.cn", "tsinghua.edu.cn"]
    assert captured["apply"] is False


def test_dispatcher_factory_uses_human_backend(tmp_path):
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
    factory = CrawlDispatcher._default_fetcher_factory(settings)
    fetcher = factory()
    assert type(fetcher).__name__ == "HumanFetcherBridge"


async def test_crawl_async_passes_resume_to_dispatcher(tmp_path, monkeypatch):
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
    captured: dict[str, object] = {}

    class _FakeDispatcher:
        def __init__(self, *, settings):
            self.settings = settings

        async def run(self, universities, *, resume=False):
            captured["universities"] = universities
            captured["resume"] = resume
            return type("Summary", (), {"success": 1, "failed": 0, "skipped": 0, "results": []})()

    monkeypatch.setattr(crawler_cli, "CrawlDispatcher", _FakeDispatcher)
    await crawler_cli._crawl_async(
        settings,
        universities=["A"],
        skip_llm_check=True,
        resume=True,
    )
    assert captured["universities"] == ["A"]
    assert captured["resume"] is True


async def test_dispatcher_fresh_mode_backs_up_only_selected_target_db(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nA,https://a.example.edu.cn/,X\nB,https://b.sample.edu.cn/,Y\n",
        encoding="utf-8",
    )
    settings = CrawlerSettings(
        websites_path=websites,
        crawler_skills_dir=tmp_path / "skills",
        university_db_dir=tmp_path / "universities",
        max_concurrency=1,
    )
    db_path_a = _university_db_path(Path(settings.university_db_dir), "https://a.example.edu.cn/")
    db_path_b = _university_db_path(Path(settings.university_db_dir), "https://b.sample.edu.cn/")
    db_path_a.parent.mkdir(parents=True, exist_ok=True)
    db_path_a.write_bytes(b"old-a")
    db_path_b.write_bytes(b"old-b")

    dispatcher = CrawlDispatcher(settings=settings, agent_factory=FakeAgent, fetcher_factory=NoopFetcher)
    summary = await dispatcher.run(universities=["A"])

    assert summary.success == 1
    assert summary.skipped == 0
    assert db_path_a.exists()
    assert db_path_a.read_bytes() != b"old-a"
    assert db_path_b.read_bytes() == b"old-b"

    backup_root = Path(settings.university_db_dir) / "backup"
    backup_dirs = list(backup_root.iterdir())
    assert len(backup_dirs) == 1
    backed_up_files = {p.name for p in backup_dirs[0].iterdir()}
    assert db_path_a.name in backed_up_files
    assert db_path_b.name not in backed_up_files
    assert (backup_dirs[0] / db_path_a.name).read_bytes() == b"old-a"


async def test_dispatcher_resume_mode_skips_completed_db_with_professors(tmp_path):
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
    )
    db_path = _university_db_path(Path(settings.university_db_dir), "https://a.example.edu.cn/")
    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        await crawler_db.ensure_runtime_schema(session)
        await crawler_db.ensure_university_meta(
            session,
            name="A",
            start_url="https://a.example.edu.cn/",
            location="X",
        )
        await crawler_db.set_university_status(session, CrawlStatus.COMPLETED)
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "title": "Professor",
                "org_unit_name": "CS",
                "org_unit_url": "https://a.example.edu.cn/cs",
                "source_url": "https://a.example.edu.cn/cs/faculty",
            },
        )
        org_unit = (await session.execute(select(OrgUnit).where(OrgUnit.name == "CS"))).scalar_one()
        polluted = Professor(
            name="Ada（兼）",
            name_key="Ada（兼）",
            org_unit_name="CS",
            homepage="https://a.example.edu.cn/cs/info/1001/ada.htm",
            bio="polluted row",
        )
        session.add(polluted)
        await session.flush()
        session.add(
            ProfessorAffiliation(
                professor_id=polluted.id,
                org_unit_id=org_unit.id,
                source_url="https://a.example.edu.cn/cs/info/1001/ada.htm",
            )
        )
    await db.close()

    dispatcher = CrawlDispatcher(settings=settings, agent_factory=FakeAgent, fetcher_factory=NoopFetcher)
    summary = await dispatcher.run(universities=["A"], resume=True)

    assert summary.success == 0
    assert summary.failed == 0
    assert summary.skipped == 1
    assert not (Path(settings.university_db_dir) / "backup").exists()

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        assert len(professors) == 1
        assert professors[0].name == "Ada"
        assert professors[0].bio == "polluted row"
    await db.close()


async def test_dispatcher_fresh_mode_aborts_when_backup_fails(tmp_path, monkeypatch):
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
    )
    db_path = _university_db_path(Path(settings.university_db_dir), "https://a.example.edu.cn/")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"old-a")

    def _raise_copy(*args, **kwargs):
        raise OSError("copy failed")

    monkeypatch.setattr(dispatcher_module.shutil, "copy2", _raise_copy)

    dispatcher = CrawlDispatcher(settings=settings, agent_factory=FakeAgent, fetcher_factory=NoopFetcher)
    with pytest.raises(RuntimeError, match="Failed to back up selected university DB files"):
        await dispatcher.run(universities=["A"])
    assert db_path.exists()
    assert db_path.read_bytes() == b"old-a"
