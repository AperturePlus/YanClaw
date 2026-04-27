from __future__ import annotations

from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.models import CrawlLogStatus, CrawlStatus, Professor, ProfessorAffiliation
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
        assert professors[0].title == "Chair Professor"

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
        # Cross-org-unit merge should be conservative (do not overwrite existing title).
        assert professors[0].title == "Professor"

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

