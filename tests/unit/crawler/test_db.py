from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, text

from agents.crawler import db as crawler_db
from agents.crawler.fetchers import FetchResult
from agents.crawler.fetchers.link_signals import LinkSignal
from agents.crawler.models import (
    Academician,
    CrawlExtractionFailure,
    CrawlLogStatus,
    CrawlStatus,
    CrawlTask,
    CrawlTaskKind,
    CrawlTaskStatus,
    OrgUnit,
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
        assert professors[0].external_link is None
        assert professors[0].homepage == "https://cs.testu.edu.cn/ada"
        # Cross-org-unit merge should be conservative (do not overwrite existing title).
        assert professors[0].title == "教授"
        assert "Computer Science" in professors[0].org_unit_name
        assert "Software" in professors[0].org_unit_name

    await db.close()


async def test_crawl_page_cache_roundtrips_fetch_result_by_requested_and_final_url(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "page_cache.db"))
    await db.init_db()
    fetched = FetchResult(
        "https://www.example.edu.cn/final",
        "faculty text",
        ["https://www.example.edu.cn/final/detail"],
        200,
        link_signals=(
            LinkSignal(
                url="https://www.example.edu.cn/final/detail",
                anchor_text="Ada",
                heading_text="Teachers",
                parent_tags_or_classes=("nav.menu",),
                link_order=3,
            ),
        ),
    )

    async with db.session() as session:
        await crawler_db.upsert_page_cache(
            session,
            url="https://www.example.edu.cn/original",
            fetched=fetched,
        )

    async with db.session() as session:
        by_original = await crawler_db.get_cached_fetch_result(
            session,
            "https://www.example.edu.cn/original",
        )
        by_final = await crawler_db.get_cached_fetch_result(
            session,
            "https://www.example.edu.cn/final",
        )

    assert by_original is not None
    assert by_final is not None
    assert by_original.url == "https://www.example.edu.cn/final"
    assert by_final.text == "faculty text"
    assert by_final.links == ["https://www.example.edu.cn/final/detail"]
    assert by_final.link_signals[0].anchor_text == "Ada"
    assert by_final.link_signals[0].parent_tags_or_classes == ("nav.menu",)

    await db.close()


async def test_upsert_professor_dedupes_same_org_unit_by_name_key(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "name_key_professor.db"))
    await db.init_db()

    async with db.session() as session:
        first = await crawler_db.upsert_professor(
            session,
            {
                "name": "王俊",
                "org_unit_name": "软件学院",
                "org_unit_url": "https://soft.example.edu.cn/",
                "title": "Professor",
                "source_url": "https://soft.example.edu.cn/szdw.htm",
            },
        )
        second = await crawler_db.upsert_professor(
            session,
            {
                "name": "王 俊",
                "org_unit_name": "软件学院",
                "org_unit_url": "https://soft.example.edu.cn/",
                "email": "wangjun@example.edu.cn",
                "source_url": "https://soft.example.edu.cn/info/1001/1.htm",
            },
        )
        assert first.id == second.id

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        affiliations = (await session.execute(select(ProfessorAffiliation))).scalars().all()
        assert len(professors) == 1
        assert len(affiliations) == 1
        assert professors[0].name == "王俊"
        assert professors[0].name_key == "王俊"
        assert professors[0].email == "wangjun@example.edu.cn"
        assert affiliations[0].source_url == "https://soft.example.edu.cn/info/1001/1.htm"

    await db.close()


async def test_upsert_professor_strips_low_value_name_marker_before_deduping(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "name_marker_professor.db"))
    await db.init_db()

    async with db.session() as session:
        first = await crawler_db.upsert_professor(
            session,
            {
                "name": "王俊（兼）",
                "org_unit_name": "软件学院",
                "org_unit_url": "https://soft.example.edu.cn/",
                "title": "Professor",
                "source_url": "https://soft.example.edu.cn/szdw.htm",
            },
        )
        second = await crawler_db.upsert_professor(
            session,
            {
                "name": "王俊",
                "org_unit_name": "软件学院",
                "org_unit_url": "https://soft.example.edu.cn/",
                "homepage": "https://soft.example.edu.cn/info/1001/1.htm",
            },
        )
        assert first.id == second.id

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        assert len(professors) == 1
        assert professors[0].name == "王俊"
        assert professors[0].name_key == "王俊"
        assert professors[0].homepage == "https://soft.example.edu.cn/info/1001/1.htm"

    await db.close()


async def test_upsert_professor_dedupes_cross_org_unit_by_homepage(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "homepage_dedupe.db"))
    await db.init_db()

    async with db.session() as session:
        first = await crawler_db.upsert_professor_with_status(
            session,
            {
                "name": "王俊（兼）",
                "org_unit_name": "软件学院",
                "org_unit_url": "https://soft.example.edu.cn/",
                "homepage": "https://soft.example.edu.cn/info/1001/1.htm#top",
                "title": "Professor",
            },
        )
        second = await crawler_db.upsert_professor_with_status(
            session,
            {
                "name": "王俊",
                "org_unit_name": "计算机学院",
                "org_unit_url": "https://cs.example.edu.cn/",
                "homepage": "https://soft.example.edu.cn/info/1001/1.htm/",
                "email": "wangjun@example.edu.cn",
            },
        )
        assert first.entity.id == second.entity.id
        assert second.deduped_by_homepage is True

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        affiliations = (await session.execute(select(ProfessorAffiliation))).scalars().all()
        assert len(professors) == 1
        assert len(affiliations) == 2
        assert professors[0].name == "王俊"
        assert professors[0].homepage == "https://soft.example.edu.cn/info/1001/1.htm"
        assert professors[0].email == "wangjun@example.edu.cn"
        assert "软件学院" in professors[0].org_unit_name
        assert "计算机学院" in professors[0].org_unit_name

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


async def test_upsert_academician_dedupes_same_org_unit_by_name_key(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "name_key_academician.db"))
    await db.init_db()

    async with db.session() as session:
        first = await crawler_db.upsert_academician(
            session,
            {
                "name": "汪莎",
                "org_unit_name": "材料学院",
                "org_unit_url": "https://mat.example.edu.cn/",
                "title": "Academician",
            },
        )
        second = await crawler_db.upsert_academician(
            session,
            {
                "name": "汪　莎",
                "org_unit_name": "材料学院",
                "org_unit_url": "https://mat.example.edu.cn/",
                "email": "wangsha@example.edu.cn",
            },
        )
        assert first.id == second.id

    async with db.session() as session:
        academicians = (await session.execute(select(Academician))).scalars().all()
        assert len(academicians) == 1
        assert academicians[0].name == "汪莎"
        assert academicians[0].name_key == "汪莎"
        assert academicians[0].email == "wangsha@example.edu.cn"

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
                homepage="https://cs.example.edu.cn/info/1001/1.htm",
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
        assert professor.homepage == "https://cs.example.edu.cn/info/1001/1.htm"

    await db.close()


async def test_ensure_runtime_schema_repairs_professor_name_key_pollution(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "schema_name_key_repair.db"))
    await db.init_db()

    async with db.session() as session:
        org_unit = OrgUnit(name="软件学院", url="https://soft.example.edu.cn/", kind="college")
        session.add(org_unit)
        await session.flush()
        clean = Professor(
            name="王俊",
            name_key="",
            org_unit_name="软件学院",
            title="教授",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        polluted = Professor(
            name="王 俊",
            name_key="",
            org_unit_name="软件学院",
            email="wangjun@example.edu.cn",
            bio="研究软件工程",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add_all([clean, polluted])
        await session.flush()
        session.add_all(
            [
                ProfessorAffiliation(
                    professor_id=clean.id,
                    org_unit_id=org_unit.id,
                    source_url="https://soft.example.edu.cn/szdw.htm",
                    created_at=datetime.now(timezone.utc),
                ),
                ProfessorAffiliation(
                    professor_id=polluted.id,
                    org_unit_id=org_unit.id,
                    source_url="https://soft.example.edu.cn/info/1001/1.htm",
                    created_at=datetime.now(timezone.utc),
                ),
            ]
        )

    async with db.session() as session:
        await crawler_db.ensure_runtime_schema(session)

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        affiliations = (await session.execute(select(ProfessorAffiliation))).scalars().all()
        assert len(professors) == 1
        assert len(affiliations) == 1
        assert professors[0].name == "王俊"
        assert professors[0].name_key == "王俊"
        assert professors[0].email == "wangjun@example.edu.cn"
        assert professors[0].bio == "研究软件工程"
        assert affiliations[0].source_url == "https://soft.example.edu.cn/info/1001/1.htm"

    await db.close()


async def test_ensure_runtime_schema_repairs_professor_homepage_pollution(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "schema_homepage_repair.db"))
    await db.init_db()

    async with db.session() as session:
        soft = OrgUnit(name="软件学院", url="https://soft.example.edu.cn/", kind="college")
        cs = OrgUnit(name="计算机学院", url="https://cs.example.edu.cn/", kind="college")
        session.add_all([soft, cs])
        await session.flush()
        soft_id = int(soft.id)
        cs_id = int(cs.id)
        keeper = Professor(
            name="王俊（兼）",
            name_key="王俊（兼）",
            org_unit_name="软件学院",
            title="教授",
            homepage="https://SOFT.example.edu.cn/info/1001/1.htm#profile",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        victim = Professor(
            name="王俊",
            name_key="王俊",
            org_unit_name="计算机学院",
            email="wangjun@example.edu.cn",
            homepage="https://soft.example.edu.cn/info/1001/1.htm/",
            bio="研究软件工程",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        list_homepage = Professor(
            name="列表污染",
            name_key="列表污染",
            org_unit_name="软件学院",
            homepage="https://soft.example.edu.cn/szdw.htm",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add_all([keeper, victim, list_homepage])
        await session.flush()
        session.add_all(
            [
                ProfessorAffiliation(
                    professor_id=keeper.id,
                    org_unit_id=soft_id,
                    source_url="https://soft.example.edu.cn/szdw.htm",
                    created_at=datetime.now(timezone.utc),
                ),
                ProfessorAffiliation(
                    professor_id=victim.id,
                    org_unit_id=cs_id,
                    source_url="https://soft.example.edu.cn/info/1001/1.htm",
                    created_at=datetime.now(timezone.utc),
                ),
            ]
        )

    async with db.session() as session:
        await crawler_db.ensure_runtime_schema(session)

    async with db.session() as session:
        professors = (await session.execute(select(Professor).order_by(Professor.id.asc()))).scalars().all()
        wang = [row for row in professors if row.name == "王俊"]
        affiliations = (await session.execute(select(ProfessorAffiliation))).scalars().all()
        indexes = (await session.execute(text("PRAGMA index_list(professors)"))).fetchall()
        assert len(wang) == 1
        assert len(professors) == 2
        assert len(affiliations) == 2
        assert wang[0].name_key == "王俊"
        assert wang[0].email == "wangjun@example.edu.cn"
        assert wang[0].bio == "研究软件工程"
        assert wang[0].homepage == "https://soft.example.edu.cn/info/1001/1.htm"
        assert {aff.org_unit_id for aff in affiliations} == {soft_id, cs_id}
        polluted = next(row for row in professors if row.name == "列表污染")
        assert polluted.homepage is None
        assert any(row[1] == "uq_professors_homepage" for row in indexes)

    await db.close()


async def test_cleanup_excluded_org_units_removes_related_records(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "excluded_org_cleanup.db"))
    await db.init_db()

    async with db.session() as session:
        cs = await crawler_db.get_or_create_org_unit(
            session,
            name="计算机学院",
            url="https://cs.example.edu.cn/",
            kind="college",
        )
        art = await crawler_db.get_or_create_org_unit(
            session,
            name="艺术学院",
            url="https://art.example.edu.cn/",
            kind="college",
        )
        pitt = await crawler_db.get_or_create_org_unit(
            session,
            name="匹兹堡学院",
            url="https://pitt.example.edu.cn/",
            kind="college",
        )
        cs_id = int(cs.id)
        art_id = int(art.id)
        pitt_id = int(pitt.id)

        cs_professor = Professor(
            name="CS Only",
            name_key="csonly",
            org_unit_name="计算机学院",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        art_professor = Professor(
            name="Art Only",
            name_key="artonly",
            org_unit_name="艺术学院",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        shared_professor = Professor(
            name="Shared",
            name_key="shared",
            org_unit_name="计算机学院 / 艺术学院",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add_all([cs_professor, art_professor, shared_professor])
        await session.flush()
        session.add_all(
            [
                ProfessorAffiliation(professor_id=cs_professor.id, org_unit_id=cs_id),
                ProfessorAffiliation(professor_id=art_professor.id, org_unit_id=art_id),
                ProfessorAffiliation(professor_id=shared_professor.id, org_unit_id=cs_id),
                ProfessorAffiliation(professor_id=shared_professor.id, org_unit_id=art_id),
            ]
        )
        session.add_all(
            [
                Academician(
                    name="CS Academician",
                    name_key="csacademician",
                    org_unit_id=cs_id,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                ),
                Academician(
                    name="Art Academician",
                    name_key="artacademician",
                    org_unit_id=art_id,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                ),
            ]
        )
        keep_task = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="计算机学院",
            org_unit_url="https://cs.example.edu.cn/",
            source_url="https://cs.example.edu.cn/szdw.htm",
            page_url="https://cs.example.edu.cn/szdw.htm",
            page_hash="cs",
            page_text_snapshot="cs",
            allowed_tools="save_professors",
        )
        art_task = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="艺术学院",
            org_unit_url="https://art.example.edu.cn/",
            source_url="https://art.example.edu.cn/szdw.htm",
            page_url="https://art.example.edu.cn/szdw.htm",
            page_hash="art",
            page_text_snapshot="art",
            allowed_tools="save_professors",
        )
        pitt_task = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="匹兹堡学院",
            org_unit_url="https://pitt.example.edu.cn/",
            source_url="https://pitt.example.edu.cn/szdw.htm",
            page_url="https://pitt.example.edu.cn/szdw.htm",
            page_hash="pitt",
            page_text_snapshot="pitt",
            allowed_tools="save_professors",
        )
        await crawler_db.log_extraction_failure(
            session,
            task_id=int(art_task.id),
            failure_type="invalid_json",
            org_unit_name="艺术学院",
            source_url="https://art.example.edu.cn/szdw.htm",
        )
        await crawler_db.log_extraction_failure(
            session,
            task_id=int(pitt_task.id),
            failure_type="invalid_json",
            org_unit_name="匹兹堡学院",
            source_url="https://pitt.example.edu.cn/szdw.htm",
        )

        summary = await crawler_db.cleanup_excluded_org_units(session, [art, pitt])

    assert summary["org_units_deleted"] == 2
    assert summary["affiliations_deleted"] == 2
    assert summary["professors_deleted"] == 1
    assert summary["professors_updated"] == 1
    assert summary["academicians_deleted"] == 1
    assert summary["crawl_tasks_deleted"] == 2
    assert summary["crawl_extraction_failures_deleted"] == 2

    async with db.session() as session:
        org_units = (await session.execute(select(OrgUnit))).scalars().all()
        professors = (await session.execute(select(Professor).order_by(Professor.name))).scalars().all()
        affiliations = (await session.execute(select(ProfessorAffiliation))).scalars().all()
        academicians = (await session.execute(select(Academician))).scalars().all()
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()

        assert [row.name for row in org_units] == ["计算机学院"]
        assert [row.name for row in professors] == ["CS Only", "Shared"]
        assert next(row for row in professors if row.name == "Shared").org_unit_name == "计算机学院"
        assert {row.org_unit_id for row in affiliations} == {cs_id}
        assert [row.name for row in academicians] == ["CS Academician"]
        assert [row.id for row in tasks] == [keep_task.id]
        assert failures == []

    await db.close()


async def test_retryable_fetch_failure_urls_only_include_unresolved_fetch_failures(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "retryable_fetch_failures.db"))
    await db.init_db()

    async with db.session() as session:
        await crawler_db.upsert_page_cache(
            session,
            url="https://timeout.example.edu.cn/",
            fetched=FetchResult(
                "https://timeout.example.edu.cn/",
                "",
                [],
                0,
                block_reason="timeout",
            ),
        )
        await crawler_db.upsert_page_cache(
            session,
            url="https://skip.example.edu.cn/",
            fetched=FetchResult(
                "https://skip.example.edu.cn/",
                "",
                [],
                0,
                block_reason="human_skip",
            ),
        )
        await crawler_db.upsert_page_cache(
            session,
            url="https://invalid.example.edu.cn/",
            fetched=FetchResult(
                "https://invalid.example.edu.cn/",
                "",
                [],
                0,
                block_reason="invalid_url",
            ),
        )
        await crawler_db.log_crawl(
            session,
            "https://blocked.example.edu.cn/",
            CrawlLogStatus.FAILED,
            "depth=1 status_code=0 blocked=timeout links=0",
        )
        await crawler_db.log_crawl(
            session,
            "https://empty.example.edu.cn/",
            CrawlLogStatus.FAILED,
            "no_structured_data",
        )
        await crawler_db.log_crawl(
            session,
            "https://resolved.example.edu.cn/",
            CrawlLogStatus.FAILED,
            "depth=1 status_code=0 blocked=timeout links=0",
        )
        await crawler_db.log_crawl(
            session,
            "https://resolved.example.edu.cn/",
            CrawlLogStatus.SUCCESS,
            "depth=1 status_code=200",
        )

        urls = await crawler_db.list_retryable_fetch_failure_urls(session)
        count = await crawler_db.count_retryable_fetch_failure_urls(session)

    assert urls == ["https://blocked.example.edu.cn", "https://timeout.example.edu.cn"]
    assert count == 2

    async with db.session() as session:
        await crawler_db.upsert_page_cache(
            session,
            url="https://timeout.example.edu.cn/",
            fetched=FetchResult(
                "https://timeout.example.edu.cn/",
                "ok",
                [],
                200,
            ),
        )
        urls = await crawler_db.list_retryable_fetch_failure_urls(session)

    assert urls == ["https://blocked.example.edu.cn"]
    await db.close()


async def test_upsert_professor_assigns_external_link_without_overwriting_homepage_with_list_page(tmp_path):
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
        assert professor.homepage is None
        assert professor.external_link == "https://external.example.com/ada"

    await db.close()


async def test_upsert_professor_prefers_detail_homepage_and_prevents_downgrade_to_list_page(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "homepage_downgrade.db"))
    await db.init_db()

    async with db.session() as session:
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "CS",
                "org_unit_url": "https://cs.testu.edu.cn/",
                "source_url": "https://cs.testu.edu.cn/info/1001/1.htm",
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "CS",
                "org_unit_url": "https://cs.testu.edu.cn/",
                "source_url": "https://cs.testu.edu.cn/szdw.htm",
            },
        )

    async with db.session() as session:
        professor = (await session.execute(select(Professor))).scalar_one()
        assert professor.homepage == "https://cs.testu.edu.cn/info/1001/1.htm"
        affiliation = (await session.execute(select(ProfessorAffiliation))).scalar_one()
        assert affiliation.source_url == "https://cs.testu.edu.cn/info/1001/1.htm"

    await db.close()


async def test_upsert_professor_separates_internal_homepage_and_external_link(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "homepage_external_split.db"))
    await db.init_db()

    async with db.session() as session:
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "CS",
                "org_unit_url": "https://cs.testu.edu.cn/",
                "source_url": "https://cs.testu.edu.cn/info/1001/1.htm",
                "homepage": "https://cs.testu.edu.cn/info/1001/1.htm",
                "external_link": "https://scholar.google.com/citations?user=ada",
            },
        )

    async with db.session() as session:
        professor = (await session.execute(select(Professor))).scalar_one()
        assert professor.homepage == "https://cs.testu.edu.cn/info/1001/1.htm"
        assert professor.external_link == "https://scholar.google.com/citations?user=ada"

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


async def test_org_unit_homepage_is_not_overwritten_by_deep_faculty_page(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "org_unit_url.db"))
    await db.init_db()

    async with db.session() as session:
        await crawler_db.get_or_create_org_unit(
            session,
            name="计算机学院",
            url="https://scse.buaa.edu.cn/",
            kind="college",
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "计算机学院",
                "org_unit_url": "https://scse.buaa.edu.cn/szdw/qtjs/5.htm",
                "source_url": "https://scse.buaa.edu.cn/info/1078/2627.htm",
                "title": "Professor",
            },
        )

    async with db.session() as session:
        org_unit = (await session.execute(select(OrgUnit).where(OrgUnit.name == "计算机学院"))).scalar_one()
        assert org_unit.url == "https://scse.buaa.edu.cn"

    await db.close()


async def test_upsert_crawl_task_does_not_reset_active_or_terminal_statuses(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "task_status_reset.db"))
    await db.init_db()

    async with db.session() as session:
        for status in (
            CrawlTaskStatus.DONE,
            CrawlTaskStatus.FAILED,
            CrawlTaskStatus.IN_PROGRESS,
            CrawlTaskStatus.RETRY,
        ):
            source_url = f"https://soft.buaa.edu.cn/{status.value}.htm"
            created = await crawler_db.upsert_crawl_task(
                session,
                university="北京航空航天大学",
                org_unit_name="软件学院",
                org_unit_url="https://soft.buaa.edu.cn/",
                source_url=source_url,
                page_url=source_url,
                page_hash=f"hash-{status.value}",
                page_text_snapshot="师资队伍",
                allowed_tools='["save_professors"]',
                status=CrawlTaskStatus.PENDING,
            )
            await crawler_db.set_crawl_task_status(session, created.id, status=status)
            updated = await crawler_db.upsert_crawl_task(
                session,
                university="北京航空航天大学",
                org_unit_name="软件学院",
                org_unit_url="https://soft.buaa.edu.cn/",
                source_url=source_url,
                page_url=source_url,
                page_hash=f"hash-{status.value}",
                page_text_snapshot="师资队伍 教授 副教授",
                allowed_tools='["save_professors"]',
                status=CrawlTaskStatus.PENDING,
            )
            assert updated.status == status.value
            assert updated.task_kind == CrawlTaskKind.LIST_PAGE.value

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
            task_kind=CrawlTaskKind.DETAIL_PAGE,
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
        assert recovered[0].task_kind == CrawlTaskKind.DETAIL_PAGE.value
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


async def test_stale_in_progress_crawl_tasks_are_recovered_with_task_kind_preserved(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "stale_tasks.db"))
    await db.init_db()

    async with db.session() as session:
        list_task = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="Computer Science",
            org_unit_url="https://cs.testu.edu.cn/",
            source_url="https://cs.testu.edu.cn/faculty.htm",
            page_url="https://cs.testu.edu.cn/faculty.htm",
            page_hash="list-hash",
            task_kind=CrawlTaskKind.LIST_PAGE,
            page_text_snapshot="faculty list",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )
        detail_task = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="Computer Science",
            org_unit_url="https://cs.testu.edu.cn/",
            source_url="https://cs.testu.edu.cn/info/1001/1.htm",
            page_url="https://cs.testu.edu.cn/info/1001/1.htm",
            page_hash="detail-hash",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot="Ada profile",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )
        await crawler_db.set_crawl_task_status(session, list_task.id, status=CrawlTaskStatus.IN_PROGRESS)
        await crawler_db.set_crawl_task_status(session, detail_task.id, status=CrawlTaskStatus.IN_PROGRESS)

    async with db.session() as session:
        recovered_count = await crawler_db.recover_stale_in_progress_crawl_tasks(session)
        recovered = await crawler_db.list_recoverable_crawl_tasks(session, limit=20)
        assert recovered_count == 2
        assert {task.task_kind for task in recovered} == {
            CrawlTaskKind.LIST_PAGE.value,
            CrawlTaskKind.DETAIL_PAGE.value,
        }
        assert all(task.status == CrawlTaskStatus.RETRY.value for task in recovered)
        assert all(task.last_error == "recovered_stale_in_progress" for task in recovered)

    await db.close()


async def test_detail_crawl_task_dedupes_by_url_org_unit_and_kind(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "detail_task_dedup.db"))
    await db.init_db()

    async with db.session() as session:
        first = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="Computer Science",
            org_unit_url="https://cs.testu.edu.cn/",
            source_url="https://cs.testu.edu.cn/info/1001/1.htm",
            page_url="https://cs.testu.edu.cn/info/1001/1.htm",
            page_hash="hash-short",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot="Ada Professor",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )
        second = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="Computer Science",
            org_unit_url="https://cs.testu.edu.cn/",
            source_url="https://cs.testu.edu.cn/info/1001/1.htm",
            page_url="https://cs.testu.edu.cn/info/1001/1.htm",
            page_hash="hash-longer",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot="Ada Professor email ada@testu.edu.cn research systems",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )
        rows = (await session.execute(select(CrawlTask))).scalars().all()

    assert first.id == second.id
    assert len(rows) == 1
    assert rows[0].page_hash == "hash-longer"
    assert "ada@testu.edu.cn" in rows[0].page_text_snapshot
    await db.close()


async def test_crawl_task_upsert_returns_conflicting_unique_task_without_integrity_error(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "task_unique_conflict.db"))
    await db.init_db()
    source_url = "https://cs.testu.edu.cn/szdw.htm"

    async with db.session() as session:
        detail = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="Computer Science",
            org_unit_url="https://cs.testu.edu.cn/",
            source_url=source_url,
            page_url=source_url,
            page_hash="detail-old",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot="short",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.FAILED,
        )
        conflict = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="Computer Science",
            org_unit_url="https://cs.testu.edu.cn/",
            source_url=source_url,
            page_url=source_url,
            page_hash="list-current",
            task_kind=CrawlTaskKind.LIST_PAGE,
            page_text_snapshot="faculty list current",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.DONE,
        )
        returned = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="Computer Science",
            org_unit_url="https://cs.testu.edu.cn/",
            source_url=source_url,
            page_url=source_url,
            page_hash="list-current",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot="faculty list current with a longer browser overlay snapshot",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )
        rows = (await session.execute(select(CrawlTask).order_by(CrawlTask.id))).scalars().all()

    assert returned.id == conflict.id
    assert len(rows) == 2
    assert rows[0].id == detail.id
    assert rows[0].page_hash == "detail-old"
    assert rows[0].task_kind == CrawlTaskKind.DETAIL_PAGE.value
    assert rows[1].id == conflict.id
    assert rows[1].page_hash == "list-current"
    assert rows[1].task_kind == CrawlTaskKind.LIST_PAGE.value
    assert rows[1].status == CrawlTaskStatus.DONE.value
    await db.close()


async def test_crawl_task_upsert_exact_match_does_not_rewrite_task_kind(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "task_kind_exact_match.db"))
    await db.init_db()
    source_url = "https://cs.testu.edu.cn/info/1001/1.htm"

    async with db.session() as session:
        list_task = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="Computer Science",
            org_unit_url="https://cs.testu.edu.cn/",
            source_url=source_url,
            page_url=source_url,
            page_hash="same-content-hash",
            task_kind=CrawlTaskKind.LIST_PAGE,
            page_text_snapshot="same page text",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.DONE,
        )
        returned = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="Computer Science",
            org_unit_url="https://cs.testu.edu.cn/",
            source_url=source_url,
            page_url=source_url,
            page_hash="same-content-hash",
            task_kind=CrawlTaskKind.DETAIL_PAGE,
            page_text_snapshot="same page text",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING,
        )
        row = (await session.execute(select(CrawlTask))).scalar_one()

    assert returned.id == list_task.id
    assert row.task_kind == CrawlTaskKind.LIST_PAGE.value
    assert row.status == CrawlTaskStatus.DONE.value
    await db.close()
