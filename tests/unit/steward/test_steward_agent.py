from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.config import CrawlerSettings
from agents.crawler.models import CrawlTask, DataQualityAudit, OrgUnit, Professor, ProfessorAffiliation, StewardRun
from agents.data_steward.agent import DataStewardAgent
from runtime.database import DatabaseManager


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
