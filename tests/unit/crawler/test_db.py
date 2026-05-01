from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.models import (
    CrawlExtractionFailure,
    CrawlLogStatus,
    CrawlStatus,
    CrawlTask,
    CrawlTaskStatus,
    Professor,
    ProfessorAffiliation,
)
from runtime.database import DatabaseManager
from tests.conftest import sqlite_url


async def test_crawler_db_upsert_dedupe_status_and_targets(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "crawler.db"))
    await db.init_db()
    csv_path = tmp_path / "websites.csv"
    csv_path.write_text(
        "index,name,url,location\n1,TestU,<https://www.example.edu.cn/,City>\n",
        encoding="utf-8",
    )

    targets = crawler_db.load_university_targets_from_csv(csv_path)
    assert targets[0]["name"] == "TestU"
    assert targets[0]["url"] == "https://www.example.edu.cn/"
    assert targets[0]["location"] == "City"

    async with db.session() as session:
        await crawler_db.ensure_university_meta(
            session,
            name="TestU",
            start_url="https://www.example.edu.cn/",
            location="City",
        )
        await crawler_db.log_crawl(
            session,
            "https://www.example.edu.cn/page",
            CrawlLogStatus.SUCCESS,
            "ok",
        )
        assert await crawler_db.is_url_crawled(session, "https://www.example.edu.cn/page")
        await crawler_db.set_university_status(session, CrawlStatus.IN_PROGRESS)
        assert await crawler_db.get_university_status(session) == CrawlStatus.IN_PROGRESS

        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "CS",
                "org_unit_url": "https://cs.example.edu.cn/",
                "title": "Professor",
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "CS",
                "org_unit_url": "https://cs.example.edu.cn/",
                "title": "Chair Professor",
            },
        )

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        assert len(professors) == 1
        assert professors[0].title == "教授"
        assert professors[0].org_unit_name == "CS"

    await db.close()


async def test_upsert_professor_dedupes_cross_org_unit_by_email_and_tracks_affiliations(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "dedupe.db"))
    await db.init_db()

    async with db.session() as session:
        first = await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "Computer Science",
                "org_unit_url": "https://cs.testu.edu.cn/",
                "email": "Ada@TestU.edu.cn ",
                "homepage": "https://cs.testu.edu.cn/ada/",
                "title": "Professor",
            },
        )
        second = await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "Software",
                "org_unit_url": "https://soft.testu.edu.cn/",
                "email": "ada@testu.edu.cn",
                "homepage": "https://cs.testu.edu.cn/ada",
                "title": "Dean",
            },
        )
        assert first.id == second.id
        assert await crawler_db.count_professors(session) == 1

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        affiliations = (await session.execute(select(ProfessorAffiliation))).scalars().all()
        assert len(professors) == 1
        assert len(affiliations) == 2
        assert professors[0].email == "ada@testu.edu.cn"
        assert professors[0].external_link == "https://cs.testu.edu.cn/ada"
        assert professors[0].homepage is None
        # Cross-org-unit merge should be conservative (do not overwrite existing title).
        assert professors[0].title == "教授"
        assert "Computer Science" in professors[0].org_unit_name
        assert "Software" in professors[0].org_unit_name

    await db.close()


async def test_load_university_targets_accepts_markdown_autolink_urls(tmp_path):
    csv_path = tmp_path / "websites.md"
    csv_path.write_text(
        "index,name,url,location\n1,TestU,<https://www.example.edu.cn/>,City\n",
        encoding="utf-8",
    )
    targets = crawler_db.load_university_targets_from_csv(csv_path)
    assert targets[0]["url"] == "https://www.example.edu.cn/"
    assert targets[0]["location"] == "City"


async def test_upsert_academician_professors(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "cleanup.db"))
    await db.init_db()

    async with db.session() as session:
        await crawler_db.upsert_academician(
            session,
            {
                "name": "A",
                "org_unit_name": "CS",
                "org_unit_url": "https://cs.example.edu.cn/",
                "title": "Academician",
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "B",
                "org_unit_name": "CS",
                "org_unit_url": "https://cs.example.edu.cn/",
                "title": "Professor",
            },
        )
        assert await crawler_db.count_academicians(session) == 1
        assert await crawler_db.count_professors(session) == 1

    await db.close()


async def test_ensure_runtime_schema_normalizes_empty_professor_fields(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "schema.db"))
    await db.init_db()

    async with db.session() as session:
        session.add(
            Professor(
                name="A",
                org_unit_name="",
                title="",
                email="",
                phone="",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )

    async with db.session() as session:
        await crawler_db.ensure_runtime_schema(session)
        professor = (await session.execute(select(Professor))).scalar_one()
        assert professor.org_unit_name == "Unknown"
        assert professor.title is None
        assert professor.email is None
        assert professor.phone is None

    await db.close()


async def test_upsert_professor_homepage_is_source_url_and_external_link_is_personal_link(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "homepage_source.db"))
    await db.init_db()

    async with db.session() as session:
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "CS",
                "org_unit_url": "https://cs.testu.edu.cn/",
                "homepage": "https://external.example.com/ada",
                "source_url": "https://cs.testu.edu.cn/szdw/ada.htm",
            },
        )

    async with db.session() as session:
        professor = (await session.execute(select(Professor))).scalar_one()
        assert professor.homepage == "https://cs.testu.edu.cn/szdw/ada.htm"
        assert professor.external_link == "https://external.example.com/ada"

    await db.close()


async def test_get_or_create_org_unit_dedupes_same_name_with_different_urls(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "org_unit_name.db"))
    await db.init_db()

    async with db.session() as session:
        first = await crawler_db.get_or_create_org_unit(
            session,
            name="经济学院",
            url="https://econ1.testu.edu.cn/",
            kind="college",
        )
        second = await crawler_db.get_or_create_org_unit(
            session,
            name="经济学院",
            url="https://econ2.testu.edu.cn/",
            kind="college",
        )
        assert first.id == second.id

    await db.close()


async def test_crawl_task_recovery_and_failure_audit(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "tasks.db"))
    await db.init_db()

    async with db.session() as session:
        created = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="Computer Science",
            org_unit_url="https://cs.testu.edu.cn/",
            source_url="https://cs.testu.edu.cn/info/1001/1.htm",
            page_url="https://cs.testu.edu.cn/info/1001/1.htm",
            page_hash="abc123",
            page_text_snapshot="sample text",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )
        await crawler_db.set_crawl_task_status(
            session,
            created.id,
            status=CrawlTaskStatus.IN_PROGRESS,
            attempt=1,
            last_error="timeout",
        )
        await crawler_db.log_extraction_failure(
            session,
            task_id=created.id,
            failure_type="invalid_json",
            org_unit_name="Computer Science",
            source_url="https://cs.testu.edu.cn/info/1001/1.htm",
            raw_arguments_preview='{"professors":[{"name":"Ada","enrollment_pre',
            attempt=1,
            resolver="retry",
        )

    async with db.session() as session:
        recovered = await crawler_db.list_recoverable_crawl_tasks(session, limit=20)
        assert len(recovered) == 1
        assert recovered[0].status == CrawlTaskStatus.RETRY.value
        summary = await crawler_db.summarize_crawl_task_status(session)
        assert summary[CrawlTaskStatus.RETRY.value] == 1
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()
        assert len(failures) == 1
        assert failures[0].failure_type == "invalid_json"

    async with db.session() as session:
        await crawler_db.set_crawl_task_status(session, recovered[0].id, status=CrawlTaskStatus.DONE)
        done = (await session.execute(select(CrawlTask))).scalar_one()
        assert done.status == CrawlTaskStatus.DONE.value

    await db.close()
