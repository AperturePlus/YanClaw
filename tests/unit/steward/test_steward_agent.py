from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.config import CrawlerSettings
from agents.crawler.models import (
    Academician,
    CrawlTask,
    CrawlTaskKind,
    CrawlTaskStatus,
    DataQualityAudit,
    OrgUnit,
    Professor,
    ProfessorAffiliation,
    StewardRun,
)
from agents.data_steward.agent import DataStewardAgent
from runtime.database import DatabaseManager
from runtime.llm import LLMResult


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


async def _seed_completion_detail_task(
    session,
    *,
    university: str = "TestU",
    org_unit_name: str = "CS",
    org_unit_url: str = "https://www.example.edu.cn/cs",
    url: str,
    snapshot: str,
) -> None:
    await crawler_db.upsert_crawl_task(
        session,
        university=university,
        org_unit_name=org_unit_name,
        org_unit_url=org_unit_url,
        source_url=url,
        page_url=url,
        page_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
        page_text_snapshot=snapshot,
        allowed_tools='["save_professors"]',
        task_kind=CrawlTaskKind.DETAIL_PAGE,
        status=CrawlTaskStatus.RETRY,
        priority=-10,
        last_error="completion_recrawl_missing_research_areas",
    )


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


async def _seed_sub_department_org_db(db_path: Path) -> None:
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
        await crawler_db.get_or_create_org_unit(
            session,
            name="自动化科学与电气工程学院",
            url="https://auto.example.edu.cn/",
            kind="college",
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Sub A",
                "org_unit_name": "工业互联网与建模仿真系",
                "org_unit_url": "https://auto.example.edu.cn/szdw/gongye.htm",
                "title": "Professor",
                "research_areas": "systems",
                "bio": "sub department professor",
                "source_url": "https://auto.example.edu.cn/szdw/gongye.htm",
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


async def test_steward_dry_run_audits_sub_department_sections_without_merging(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"
    await _seed_sub_department_org_db(db_path)

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

    assert summary.total_sub_department_sections_detected == 1
    assert summary.total_sub_department_sections_merged == 0

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        org_units = (await session.execute(select(OrgUnit).order_by(OrgUnit.name))).scalars().all()
        audits = (await session.execute(select(DataQualityAudit))).scalars().all()
        assert [unit.name for unit in org_units] == ["工业互联网与建模仿真系", "自动化科学与电气工程学院"]
        assert any(
            audit.issue_type == "sub_department_section" and audit.action == "report_only"
            for audit in audits
        )
    await db.close()


async def test_steward_apply_merges_sub_department_sections(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"
    await _seed_sub_department_org_db(db_path)

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

    assert summary.total_sub_department_sections_detected == 1
    assert summary.total_sub_department_sections_merged == 1
    assert summary.runs[0].org_unit_cleanup["sub_org_units_merged"] == 1

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        org_units = (await session.execute(select(OrgUnit).order_by(OrgUnit.name))).scalars().all()
        professors = (await session.execute(select(Professor).order_by(Professor.name))).scalars().all()
        audits = (await session.execute(select(DataQualityAudit))).scalars().all()
        assert [unit.name for unit in org_units] == ["自动化科学与电气工程学院"]
        assert [(professor.name, professor.org_unit_name) for professor in professors] == [
            ("Sub A", "自动化科学与电气工程学院")
        ]
        assert any(
            audit.issue_type == "sub_department_section" and audit.action == "merged"
            for audit in audits
        )
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


async def test_steward_apply_enqueues_homepage_missing_research_recrawl(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"
    homepage = "https://www.example.edu.cn/cs/info/1001/ada.htm"

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
                "homepage": homepage,
                "source_url": "https://www.example.edu.cn/cs/faculty",
                "bio": "profile exists",
            },
        )
    await db.close()

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

    assert summary.total_recrawl_tasks_upserted == 1

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        task = (await session.execute(select(CrawlTask))).scalar_one()
        audits = (await session.execute(select(DataQualityAudit))).scalars().all()
        assert task.source_url == homepage
        assert task.page_url == homepage
        assert task.task_kind == CrawlTaskKind.DETAIL_PAGE.value
        assert task.status == CrawlTaskStatus.RETRY.value
        assert task.priority == -10
        assert task.last_error == "completion_recrawl_missing_research_areas"
        assert any(
            audit.field_name == "research_areas"
            and audit.reason == "homepage_profile_incomplete"
            and audit.action == "recrawl_enqueued"
            for audit in audits
        )
    await db.close()


async def test_steward_apply_does_not_enqueue_bio_only_or_no_homepage_research(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"

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
                "name": "Bio Only",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "homepage": "https://www.example.edu.cn/cs/info/1001/bio-only.htm",
                "research_areas": "systems",
                "source_url": "https://www.example.edu.cn/cs/faculty",
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "No Homepage",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "source_url": "https://www.example.edu.cn/cs/faculty",
                "bio": "profile exists",
            },
        )
    await db.close()

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

    assert summary.total_recrawl_tasks_upserted == 0

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
        assert tasks == []
    await db.close()


async def test_steward_apply_promotes_academician_and_backfills_research_from_bio(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"

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
                "name": "李未",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "title": "教授",
                "homepage": "https://www.example.edu.cn/cs/info/liwei.htm",
                "bio": (
                    "李未，北京航空航天大学计算机学院教授，博士生导师，中国科学院院士。"
                    "李未院士在实用并发语言操作语义、形式理论序列和修正演算等方面取得了开创性研究成果。"
                ),
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Bio Research",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "homepage": "https://www.example.edu.cn/cs/info/bio-research.htm",
                "bio": "主要从事机器学习、数据挖掘研究。",
            },
        )
        await crawler_db.upsert_academician(
            session,
            {
                "name": "Academician Bio",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "bio": "Academician Bio，院士。研究方向：形式化方法、可信软件。",
            },
        )
    await db.close()

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

    assert summary.total_recrawl_tasks_upserted == 0

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        li_professors = (await session.execute(select(Professor).where(Professor.name == "李未"))).scalars().all()
        li_academician = (await session.execute(select(Academician).where(Academician.name == "李未"))).scalar_one()
        bio_research = (await session.execute(select(Professor).where(Professor.name == "Bio Research"))).scalar_one()
        academician_bio = (
            await session.execute(select(Academician).where(Academician.name == "Academician Bio"))
        ).scalar_one()
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
        audits = (await session.execute(select(DataQualityAudit))).scalars().all()

        assert li_professors == []
        assert li_academician.title == "院士"
        assert "实用并发语言操作语义" in (li_academician.research_areas or "")
        assert bio_research.research_areas == "机器学习；数据挖掘"
        assert academician_bio.research_areas == "形式化方法；可信软件"
        assert tasks == []
        assert any(audit.issue_type == "promoted_academician" for audit in audits)
        assert any(audit.issue_type == "inferred_research_areas" for audit in audits)
    await db.close()


async def test_steward_apply_promotes_academician_from_detail_snapshot(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"
    homepage = "https://www.example.edu.cn/cs/info/liwei.htm"
    snapshot = (
        "师资队伍\n李未\n2017年10月30日\n"
        "李未，北京航空航天大学计算机学院教授，博士生导师，中国科学院院士。"
        "李未院士在实用并发语言操作语义、形式理论序列和修正演算等方面取得了开创性研究成果。"
        "李未院士在我国率先倡导进行海量信息计算的理论与方法研究。"
        "[下一篇：郑志明]"
    )

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
                "name": "李未",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "title": "教授",
                "homepage": homepage,
            },
        )
        await _seed_completion_detail_task(session, url=homepage, snapshot=snapshot)
    await db.close()

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

    assert summary.total_recrawl_tasks_upserted == 0

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        li_professors = (await session.execute(select(Professor).where(Professor.name == "李未"))).scalars().all()
        li_academician = (await session.execute(select(Academician).where(Academician.name == "李未"))).scalar_one()
        task = (await session.execute(select(CrawlTask))).scalar_one()
        audits = (await session.execute(select(DataQualityAudit))).scalars().all()

        assert li_professors == []
        assert li_academician.title == "院士"
        assert "实用并发语言操作语义" in (li_academician.research_areas or "")
        assert "形式理论序列" in (li_academician.research_areas or "")
        assert "修正演算" in (li_academician.research_areas or "")
        assert "海量信息计算的理论与方法" in (li_academician.research_areas or "")
        assert task.status == CrawlTaskStatus.DONE.value
        assert task.last_error == "completion_recrawl_repaired_from_structured_evidence"
        assert any(
            audit.issue_type == "promoted_academician"
            and audit.reason == "profile_snapshot_academician_hint"
            for audit in audits
        )
    await db.close()


async def test_steward_apply_backfills_research_from_detail_snapshot(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"
    professor_homepage = "https://www.example.edu.cn/cs/info/snapshot-research.htm"
    academician_homepage = "https://www.example.edu.cn/cs/info/snapshot-academician.htm"

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
                "name": "Snapshot Research",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "homepage": professor_homepage,
            },
        )
        await crawler_db.upsert_academician(
            session,
            {
                "name": "Snapshot Academician",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "homepage": academician_homepage,
            },
        )
        await _seed_completion_detail_task(
            session,
            url=professor_homepage,
            snapshot="Snapshot Research，教授。研究方向：机器学习、数据挖掘。主持多项课题。",
        )
        await _seed_completion_detail_task(
            session,
            url=academician_homepage,
            snapshot="Snapshot Academician，院士。研究方向：形式化方法、可信软件。",
        )
    await db.close()

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

    assert summary.total_recrawl_tasks_upserted == 0

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        professor = (
            await session.execute(select(Professor).where(Professor.name == "Snapshot Research"))
        ).scalar_one()
        academician = (
            await session.execute(select(Academician).where(Academician.name == "Snapshot Academician"))
        ).scalar_one()
        tasks = (await session.execute(select(CrawlTask).order_by(CrawlTask.id))).scalars().all()
        audits = (await session.execute(select(DataQualityAudit))).scalars().all()

        assert professor.research_areas == "机器学习；数据挖掘"
        assert academician.research_areas == "形式化方法；可信软件"
        assert [task.status for task in tasks] == [CrawlTaskStatus.DONE.value, CrawlTaskStatus.DONE.value]
        assert sum(audit.reason == "profile_snapshot_inference" for audit in audits) == 2
        assert not any(
            audit.issue_type == "promoted_academician" and audit.entity_id == professor.id
            for audit in audits
        )
    await db.close()


async def test_steward_detail_snapshot_requires_matching_name(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"
    homepage = "https://www.example.edu.cn/cs/info/false-positive.htm"

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
                "name": "False Positive",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "homepage": homepage,
            },
        )
        await _seed_completion_detail_task(
            session,
            url=homepage,
            snapshot="张三，中国科学院院士。研究方向：形式化方法、可信软件。",
        )
    await db.close()

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

    assert summary.total_recrawl_tasks_upserted == 1

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        professor = (await session.execute(select(Professor).where(Professor.name == "False Positive"))).scalar_one()
        academicians = (
            await session.execute(select(Academician).where(Academician.name == "False Positive"))
        ).scalars().all()
        task = (await session.execute(select(CrawlTask))).scalar_one()
        audits = (await session.execute(select(DataQualityAudit))).scalars().all()

        assert professor.research_areas is None
        assert academicians == []
        assert task.status == CrawlTaskStatus.RETRY.value
        assert task.last_error == "completion_recrawl_missing_research_areas"
        assert not any(str(audit.reason or "").startswith("profile_snapshot_") for audit in audits)
    await db.close()


async def test_steward_apply_demotes_misclassified_academicians_with_profile_evidence(tmp_path):
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,X\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "example.edu.cn.db"

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
                "name": "REN Ziyu",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "homepage": "https://www.example.edu.cn/cs/info/ren-ziyu.htm",
                "bio": "任子宇，北京航空航天大学机械工程及自动化学院教授，国家级青年人才。研究领域包括仿生机器人、微型机器人。",
                "research_areas": "仿生机器人；微型机器人",
            },
        )
        await crawler_db.upsert_academician(
            session,
            {
                "name": "雷文强",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "homepage": "https://www.example.edu.cn/cs/info/lei.htm",
                "bio": "国家级青年人才，博士生导师。与荷兰皇家科学院院士Maarten de Rijke教授等世界一流学者合作。",
                "research_areas": "自然语言处理；信息检索",
            },
        )
        await crawler_db.upsert_academician(
            session,
            {
                "name": "李未",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
                "homepage": "https://www.example.edu.cn/cs/info/liwei.htm",
                "bio": "李未，北京航空航天大学计算机学院教授，博士生导师，中国科学院院士。",
                "research_areas": "形式理论",
            },
        )
        await crawler_db.upsert_academician(
            session,
            {
                "name": "Evidence Missing",
                "org_unit_name": "CS",
                "org_unit_url": "https://www.example.edu.cn/cs",
            },
        )
    await db.close()

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

    assert summary.total_recrawl_tasks_upserted == 0

    db = DatabaseManager(_sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        ren_professor = (await session.execute(select(Professor).where(Professor.name == "REN Ziyu"))).scalar_one()
        lei_professor = (await session.execute(select(Professor).where(Professor.name == "雷文强"))).scalar_one()
        li_academician = (await session.execute(select(Academician).where(Academician.name == "李未"))).scalar_one()
        evidence_missing = (
            await session.execute(select(Academician).where(Academician.name == "Evidence Missing"))
        ).scalar_one()
        ren_academicians = (
            await session.execute(select(Academician).where(Academician.name == "REN Ziyu"))
        ).scalars().all()
        lei_academicians = (
            await session.execute(select(Academician).where(Academician.name == "雷文强"))
        ).scalars().all()
        audits = (await session.execute(select(DataQualityAudit))).scalars().all()

        assert ren_professor.title == "教授"
        assert ren_professor.research_areas == "仿生机器人；微型机器人"
        assert lei_professor.title is None
        assert lei_professor.research_areas == "自然语言处理；信息检索"
        assert ren_academicians == []
        assert lei_academicians == []
        assert li_academician.title == "院士"
        assert evidence_missing.title == "院士"
        assert sum(audit.issue_type == "misclassified_academician" for audit in audits) == 2
        assert all(
            audit.reason == "no_self_academician_evidence"
            for audit in audits
            if audit.issue_type == "misclassified_academician"
        )
    await db.close()
