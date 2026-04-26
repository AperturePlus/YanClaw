from __future__ import annotations

from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.models import CrawlLogStatus, CrawlStatus, Professor, ProfessorAffiliation, University
from runtime.database import DatabaseManager
from tests.conftest import sqlite_url


async def test_crawler_db_upsert_dedupe_status_and_import(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "crawler.db"))
    await db.init_db()
    csv_path = tmp_path / "websites.csv"
    csv_path.write_text("name,url,location\nTestU,https://u.example,City\n", encoding="utf-8")

    async with db.session() as session:
        universities = await crawler_db.load_universities_from_csv(session, csv_path)
        university = universities[0]
        await crawler_db.log_crawl(
            session,
            university.id,
            "https://u.example/page",
            CrawlLogStatus.SUCCESS,
            "ok",
        )
        assert await crawler_db.is_url_crawled(session, "https://u.example/page")
        await crawler_db.set_university_status(session, "TestU", CrawlStatus.IN_PROGRESS)
        assert await crawler_db.get_university_status(session, "TestU") == CrawlStatus.IN_PROGRESS

        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "university_name": "TestU",
                "college_name": "CS",
                "title": "Professor",
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "university_name": "TestU",
                "college_name": "CS",
                "title": "Chair Professor",
            },
        )

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        assert len(professors) == 1
        assert professors[0].title == "Chair Professor"

    await db.close()


async def test_upsert_professor_dedupes_cross_college_by_email_and_tracks_affiliations(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "dedupe.db"))
    await db.init_db()

    async with db.session() as session:
        first = await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "university_name": "TestU",
                "college_name": "Computer Science",
                "email": "Ada@TestU.edu.cn ",
                "homepage": "https://cs.testu.edu.cn/ada/",
                "title": "Professor",
            },
        )
        second = await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "university_name": "TestU",
                "college_name": "Software",
                "email": "ada@testu.edu.cn",
                "homepage": "https://cs.testu.edu.cn/ada",
                "title": "Dean",
            },
        )
        university = (
            await session.execute(select(University).where(University.name == "TestU"))
        ).scalar_one()
        assert first.id == second.id
        assert await crawler_db.count_professors_for_university(session, university.id) == 1

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        affiliations = (await session.execute(select(ProfessorAffiliation))).scalars().all()
        assert len(professors) == 1
        assert len(affiliations) == 2
        assert professors[0].email == "ada@testu.edu.cn"
        assert professors[0].title == "Professor"

    await db.close()


async def test_load_universities_cleans_angle_bracket_urls(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "urls.db"))
    await db.init_db()
    csv_path = tmp_path / "websites.csv"
    csv_path.write_text(
        "index,name,url,location\n1,TestU,<https://www.example.edu.cn/,City>\n",
        encoding="utf-8",
    )

    async with db.session() as session:
        universities = await crawler_db.load_universities_from_csv(session, csv_path)
        assert universities[0].url == "https://www.example.edu.cn/"
        assert universities[0].location == "City"

    await db.close()


async def test_load_universities_accepts_markdown_autolink_urls(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "autolink_urls.db"))
    await db.init_db()
    csv_path = tmp_path / "websites.md"
    csv_path.write_text(
        "index,name,url,location\n1,TestU,<https://www.example.edu.cn/>,City\n",
        encoding="utf-8",
    )

    async with db.session() as session:
        universities = await crawler_db.load_universities_from_csv(session, csv_path)
        assert universities[0].url == "https://www.example.edu.cn/"
        assert universities[0].location == "City"

    await db.close()
