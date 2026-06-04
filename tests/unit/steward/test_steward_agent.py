from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.config import CrawlerSettings
from agents.crawler.models import CrawlTask, DataQualityAudit, OrgUnit, Professor, ProfessorAffiliation, StewardRun
from agents.data_steward.agent import DataStewardAgent
from runtime.database import DatabaseManager
from runtime.llm import LLMResult


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


async def _seed_db(db_path: Path) -> None:
    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        await crawler_db.ensure_runtime_schema(session, repair_identity=False)
        await crawler_db.ensure_university_meta(
            session,
            name="TestU",
            start_url="https://www.example.edu.cn/",
            location="X",
        )
        await crawler_db.upsert_academician(
            session,
            {
                "name": "Dup A",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "title": "Academician",
                "email": "dup@example.edu.cn",
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Dup A",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "title": "Professor",
                "email": "dup@example.edu.cn",
                "source_url": "https://www.example.edu.cn/cs/faculty/dup-a",
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Missing B",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "title": "Professor",
                "source_url": "https://www.example.edu.cn/cs/faculty/missing-b",
            },
        )
        await crawler_db.log_extraction_failure(
            session,
            task_id=None,
            failure_type="no_structured_data",
            org_unit_name="CS",
            source_url="https://www.example.edu.cn/cs/faculty/missing-b",
            attempt=0,
            resolver="retry",
        )
        software = await crawler_db.get_or_create_org_unit(
            session,
            name="Software",
            url="https://www.example.edu.cn/software",
            kind="college",
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Home A（兼）",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "homepage": "https://www.example.edu.cn/cs/info/home-a.htm",
                "title": "Professor",
            },
        )
        polluted = Professor(
            name="Home A",
            name_key="Home A",
            org_unit_name="Software",
            homepage="https://www.example.edu.cn/cs/info/home-a.htm#detail",
            bio="homepage duplicate",
        )
        session.add(polluted)
        await session.flush()
        session.add(
            ProfessorAffiliation(
                professor_id=polluted.id,
                org_unit_id=software.id,
                source_url="https://www.example.edu.cn/cs/info/home-a.htm",
            )
        )
    await db.close()


async def _seed_excluded_org_db(db_path: Path, *, org_unit_name: str = "继续教育学院") -> None:
    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        await crawler_db.ensure_runtime_schema(session, repair_identity=False)
        await crawler_db.ensure_university_meta(
            session,
            name="TestU",
            start_url="https://www.example.edu.cn/",
            location="X",
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "title": "Professor",
                "research_areas": "systems",
                "bio": "CS professor",
                "source_url": "https://www.example.edu.cn/cs/faculty/ada",
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Excluded A",
                "org_unit_name": org_unit_name,
                "org_unit_url": "https://excluded.example.edu.cn/",
                "title": "Professor",
                "research_areas": "teaching",
                "bio": "teaching unit professor",
                "source_url": "https://excluded.example.edu.cn/faculty/a",
            },
        )
    await db.close()


async def test_steward_dry_run_detects_duplicates_and_writes_audits(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"
    await _seed_db(db_path)

    settings = CrawlerSettings(
        websites_path=websites,
        university_db_dir=db_dir,
    )
    summary = await DataStewardAgent(settings=settings).run(
        universities=["TestU"],
        universities_file=None,
        db_roots=None,
        apply=False,
        llm_enabled=False,
        max_context_tokens=128000,
        include_backup_audit=False,
    )

    assert summary.total_duplicates_detected == 2
    assert summary.total_duplicates_deleted == 0
    assert summary.total_missing_field_audits >= 2

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        dup_professors = (
            await session.execute(select(Professor).where(Professor.name == "Dup A"))
        ).scalars().all()
        assert len(dup_professors) == 1
        runs = (await session.execute(select(StewardRun))).scalars().all()
        audits = (await session.execute(select(DataQualityAudit))).scalars().all()
        home = (await session.execute(select(Professor).where(Professor.name.like("Home A%")))).scalars().all()
        assert len(runs) == 1
        assert len(audits) >= 3
        assert len(home) == 2
        assert any(audit.issue_type == "duplicate_professor_identity" for audit in audits)
    await db.close()


async def test_steward_dry_run_audits_excluded_org_units_without_deleting(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"
    await _seed_excluded_org_db(db_path)

    settings = CrawlerSettings(
        websites_path=websites,
        university_db_dir=db_dir,
    )
    summary = await DataStewardAgent(settings=settings).run(
        universities=["TestU"],
        universities_file=None,
        db_roots=None,
        apply=False,
        llm_enabled=False,
        max_context_tokens=128000,
        include_backup_audit=False,
    )

    assert summary.total_excluded_org_units_detected == 1
    assert summary.total_excluded_org_units_deleted == 0

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        org_units = (await session.execute(select(OrgUnit).order_by(OrgUnit.name))).scalars().all()
        audits = (await session.execute(select(DataQualityAudit))).scalars().all()
        assert [unit.name for unit in org_units] == ["CS", "继续教育学院"]
        assert any(
            audit.issue_type == "excluded_org_unit" and audit.action == "report_only"
            for audit in audits
        )
    await db.close()


async def test_steward_apply_deletes_excluded_org_units(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"
    await _seed_excluded_org_db(db_path)

    settings = CrawlerSettings(
        websites_path=websites,
        university_db_dir=db_dir,
    )
    summary = await DataStewardAgent(settings=settings).run(
        universities=["TestU"],
        universities_file=None,
        db_roots=None,
        apply=True,
        llm_enabled=False,
        max_context_tokens=128000,
        include_backup_audit=False,
    )

    assert summary.total_excluded_org_units_detected == 1
    assert summary.total_excluded_org_units_deleted == 1
    assert summary.runs[0].org_unit_cleanup["org_units_deleted"] == 1

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        org_units = (await session.execute(select(OrgUnit).order_by(OrgUnit.name))).scalars().all()
        professors = (await session.execute(select(Professor).order_by(Professor.name))).scalars().all()
        audits = (await session.execute(select(DataQualityAudit))).scalars().all()
        assert [unit.name for unit in org_units] == ["CS"]
        assert [professor.name for professor in professors] == ["Ada"]
        assert any(
            audit.issue_type == "excluded_org_unit" and audit.action == "hard_deleted"
            for audit in audits
        )
    await db.close()


async def test_steward_llm_enabled_deletes_person_named_teaching_units(tmp_path, monkeypatch):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"
    await _seed_excluded_org_db(db_path, org_unit_name="钱学森学院")

    class FakeOrgUnitLLM:
        def __init__(self, *args, **kwargs):
            pass

        async def chat(self, messages, tools=None, tool_handlers=None):
            return LLMResult(
                '{"included_org_units": [], "excluded_org_units": [{"name": "钱学森学院", "url": "https://excluded.example.edu.cn/", "reason": "person_named_teaching_unit"}]}'
            )

    monkeypatch.setattr("agents.data_steward.db.pipeline.LLMClient", FakeOrgUnitLLM)
    settings = CrawlerSettings(
        websites_path=websites,
        university_db_dir=db_dir,
        openai_api_key="test-key",
    )
    summary = await DataStewardAgent(settings=settings).run(
        universities=["TestU"],
        universities_file=None,
        db_roots=None,
        apply=True,
        llm_enabled=True,
        max_context_tokens=128000,
        include_backup_audit=False,
    )

    assert summary.total_excluded_org_units_detected == 1
    assert summary.total_excluded_org_units_deleted == 1

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        org_units = (await session.execute(select(OrgUnit).order_by(OrgUnit.name))).scalars().all()
        assert [unit.name for unit in org_units] == ["CS"]
    await db.close()


async def test_steward_apply_deletes_duplicates_and_enqueues_recrawl(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"
    await _seed_db(db_path)

    settings = CrawlerSettings(
        websites_path=websites,
        university_db_dir=db_dir,
    )
    summary = await DataStewardAgent(settings=settings).run(
        universities=["TestU"],
        universities_file=None,
        db_roots=None,
        apply=True,
        llm_enabled=False,
        max_context_tokens=128000,
        include_backup_audit=False,
    )

    assert summary.total_duplicates_detected == 2
    assert summary.total_duplicates_deleted == 2
    assert summary.total_recrawl_tasks_upserted >= 1

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        dup_professors = (
            await session.execute(select(Professor).where(Professor.name == "Dup A"))
        ).scalars().all()
        assert len(dup_professors) == 0
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
        home = (await session.execute(select(Professor).where(Professor.name == "Home A"))).scalars().all()
        assert len(tasks) >= 1
        assert len(home) == 1
        assert home[0].bio == "homepage duplicate"
    await db.close()
