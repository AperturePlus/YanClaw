from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from agents.crawler import db as crawler_db
from agents.crawler.db.professors import normalize_professor_homepage
from agents.crawler.entrances import (
    EntranceManifestError,
    load_university_entrance_targets,
)
from agents.crawler.fetchers import FetchResult
from agents.crawler.fetchers.link_signals import LinkSignal
from agents.crawler.models import (
    Academician,
    CrawlExtractionFailure,
    CrawlGraphEdge,
    CrawlGraphEdgeType,
    CrawlGraphNode,
    CrawlGraphNodeStatus,
    CrawlGraphNodeType,
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


async def test_upsert_professor_upgrades_lecturer_to_medical_senior_title(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "title_upgrade.db"))
    await db.init_db()

    async with db.session() as session:
        await crawler_db.upsert_professor(
            session,
            {
                "name": "肖力",
                "org_unit_name": "医学院",
                "org_unit_url": "https://www.med.uestc.edu.cn",
                "source_url": "https://www.med.uestc.edu.cn/szdw/dsml.htm",
                "title": "讲师",
                "homepage": "https://www.med.uestc.edu.cn/info/1311/2638.htm",
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "肖力",
                "org_unit_name": "医学院",
                "org_unit_url": "https://www.med.uestc.edu.cn",
                "source_url": "https://www.med.uestc.edu.cn/info/1311/2638.htm",
                "title": "副主任医师",
                "homepage": "https://www.med.uestc.edu.cn/info/1311/2638.htm",
                "research_areas": "临床护理",
            },
        )

    async with db.session() as session:
        professor = (await session.execute(select(Professor).where(Professor.name == "肖力"))).scalar_one()
    assert professor.title == "副主任医师"
    assert professor.research_areas == "临床护理"
    await db.close()


async def test_upsert_professor_does_not_downgrade_medical_title_to_lecturer(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "title_no_downgrade.db"))
    await db.init_db()

    async with db.session() as session:
        await crawler_db.upsert_professor(
            session,
            {
                "name": "肖力",
                "org_unit_name": "医学院",
                "org_unit_url": "https://www.med.uestc.edu.cn",
                "source_url": "https://www.med.uestc.edu.cn/info/1311/2638.htm",
                "title": "副主任医师",
                "homepage": "https://www.med.uestc.edu.cn/info/1311/2638.htm",
                "research_areas": "临床护理",
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "肖力",
                "org_unit_name": "医学院",
                "org_unit_url": "https://www.med.uestc.edu.cn",
                "source_url": "https://www.med.uestc.edu.cn/szdw/dsml.htm",
                "title": "讲师",
                "homepage": "https://www.med.uestc.edu.cn/info/1311/2638.htm",
            },
        )

    async with db.session() as session:
        professor = (await session.execute(select(Professor).where(Professor.name == "肖力"))).scalar_one()
    assert professor.title == "副主任医师"
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


async def test_upsert_professor_dedupes_latin_name_with_cjk_alias_in_same_org_unit(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "latin_cjk_alias_professor.db"))
    await db.init_db()

    async with db.session() as session:
        first = await crawler_db.upsert_professor(
            session,
            {
                "name": "Keizo Fujimoto",
                "org_unit_name": "空间与地球科学学院",
                "org_unit_url": "https://sse.buaa.edu.cn/",
                "source_url": "https://sse.buaa.edu.cn/szll/bssds.htm",
                "enrollment_pref": "硕导",
            },
        )
        second = await crawler_db.upsert_professor(
            session,
            {
                "name": "Keizo Fujimoto (藤本桂三)",
                "org_unit_name": "空间与地球科学学院",
                "org_unit_url": "https://sse.buaa.edu.cn/",
                "source_url": "https://sse.buaa.edu.cn/info/1204/6490.htm",
                "title": "研究员",
                "email": "fujimoto@buaa.edu.cn",
                "research_areas": "空间物理；计算物理",
                "bio": "2017-present: 北京航空航天大学。",
            },
        )
        assert first.id == second.id

    async with db.session() as session:
        professor = (await session.execute(select(Professor))).scalar_one()
        affiliation = (await session.execute(select(ProfessorAffiliation))).scalar_one()
        assert professor.name == "Keizo Fujimoto"
        assert professor.name_key == "Keizo Fujimoto"
        assert professor.title == "研究员"
        assert professor.email == "fujimoto@buaa.edu.cn"
        assert professor.research_areas == "空间物理；计算物理"
        assert professor.bio == "2017-present: 北京航空航天大学。"
        assert professor.homepage == "https://sse.buaa.edu.cn/info/1204/6490.htm"
        assert affiliation.source_url == "https://sse.buaa.edu.cn/info/1204/6490.htm"

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


def test_load_university_entrance_targets_accepts_manual_rows_with_chinese_headers(tmp_path):
    manifest = tmp_path / "websites.md"
    manifest.write_text(
        "\n".join(
            [
                "# manual crawler entrances",
                "record_type,序号,大学名称,官网地址,所在地,学院列表入口,学院名称,学院主页,师资入口,学院类型",
                "university,1,TestU,<https://www.example.edu.cn/>,City,,,,,",
                "org_listing,,TestU,,,https://www.example.edu.cn/orgs,,,,",
                "org_unit,,TestU,,,,计算机学院,https://cs.example.edu.cn/,https://cs.example.edu.cn/faculty,college",
                "org_unit,,TestU,,,,软件学院,https://soft.example.edu.cn/,,college",
            ]
        ),
        encoding="utf-8",
    )

    targets = load_university_entrance_targets(manifest)

    assert len(targets) == 1
    assert targets[0].name == "TestU"
    assert targets[0].url == "https://www.example.edu.cn/"
    assert targets[0].location == "City"
    assert targets[0].org_unit_listing_urls == ()
    assert [unit.name for unit in targets[0].manual_org_units] == ["计算机学院", "软件学院"]
    assert targets[0].manual_org_units[0].faculty_url == "https://cs.example.edu.cn/faculty"

    legacy = crawler_db.load_university_targets_from_csv(manifest)
    assert legacy[0]["manual_org_units"][0]["name"] == "计算机学院"
    assert legacy[0]["org_unit_listing_urls"] == []


def test_load_university_entrance_targets_uses_listing_when_no_manual_org_units(tmp_path):
    manifest = tmp_path / "websites.md"
    manifest.write_text(
        "\n".join(
            [
                "record_type,name,url,location,org_unit_listing_url,org_unit_name,org_unit_url,faculty_url",
                "university,TestU,https://www.example.edu.cn/,City,,,,",
                "org_listing,TestU,,,https://www.example.edu.cn/orgs,,,",
            ]
        ),
        encoding="utf-8",
    )

    targets = load_university_entrance_targets(manifest)

    assert targets[0].manual_org_units == ()
    assert targets[0].org_unit_listing_urls == ("https://www.example.edu.cn/orgs",)


def test_load_university_entrance_targets_accepts_yaml_manifest(tmp_path):
    manifest = tmp_path / "entrances.yaml"
    manifest.write_text(
        """
version: 1
universities:
  - name: ListingU
    url: https://www.listing.example.edu.cn/
    location: CityA
    org_unit_listing_urls:
      - https://www.listing.example.edu.cn/orgs
  - name: ManualU
    url: https://www.manual.example.edu.cn/
    location: CityB
    org_unit_listing_urls:
      - https://www.manual.example.edu.cn/ignored-when-org-units-exist
    org_units:
      - name: 机械工程学院
        url: https://mec.manual.example.edu.cn/
        kind: college
        faculty_urls:
          - https://mec.manual.example.edu.cn/szdw/jsml.htm
          - https://mec.manual.example.edu.cn/szdw/qtjs.htm
      - name: 软件学院
        url: https://se.manual.example.edu.cn/
""".strip(),
        encoding="utf-8",
    )

    targets = load_university_entrance_targets(manifest)
    by_name = {target.name: target for target in targets}

    assert by_name["ListingU"].org_unit_listing_urls == ("https://www.listing.example.edu.cn/orgs",)
    assert by_name["ListingU"].manual_org_units == ()
    assert by_name["ManualU"].org_unit_listing_urls == ()
    assert [unit.name for unit in by_name["ManualU"].manual_org_units] == [
        "机械工程学院",
        "机械工程学院",
        "软件学院",
    ]
    assert [unit.faculty_url for unit in by_name["ManualU"].manual_org_units] == [
        "https://mec.manual.example.edu.cn/szdw/jsml.htm",
        "https://mec.manual.example.edu.cn/szdw/qtjs.htm",
        "",
    ]
    assert all(unit.raw_name == "" and unit.aliases == () for unit in by_name["ManualU"].manual_org_units)

    legacy = crawler_db.load_university_targets_from_csv(manifest)
    assert legacy[1]["manual_org_units"][0]["name"] == "机械工程学院"


def test_text_manifest_is_rejected(tmp_path):
    manifest = tmp_path / "收集.txt"
    manifest.write_text("学院列表\n北京航空航天大学：https://www.buaa.edu.cn/jgsz/jxkyjg.htm\n", encoding="utf-8")

    with pytest.raises(EntranceManifestError, match="Text entrance manifest is no longer parsed"):
        load_university_entrance_targets(manifest)


def test_default_yaml_manifest_loads_key_manual_and_listing_targets():
    targets = load_university_entrance_targets("assets/entrances.yaml")
    by_name = {target.name: target for target in targets}

    assert "北京航空航天大学" in by_name
    assert by_name["北京航空航天大学"].org_unit_listing_urls == (
        "https://www.buaa.edu.cn/jgsz/jxkyjg.htm",
    )
    assert "西安交通大学" in by_name
    assert len(by_name["西安交通大学"].manual_org_units) >= 30
    xjtu_names = {unit.name for unit in by_name["西安交通大学"].manual_org_units}
    assert {
        "机械工程学院",
        "能源与动力工程学院",
        "公共卫生学院",
        "马克思主义学院",
        "新闻与新媒体学院",
    }.issubset(xjtu_names)
    assert "中山大学" in by_name
    sysu_names = {unit.name for unit in by_name["中山大学"].manual_org_units}
    assert {"珠海-数学学院", "广州-数学学院", "深圳-药学院"}.issubset(sysu_names)
    assert "厦门大学" in by_name
    assert "数学学院" in {unit.name for unit in by_name["厦门大学"].manual_org_units}


def test_load_university_entrance_targets_rejects_duplicate_org_unit_faculty_url(tmp_path):
    manifest = tmp_path / "entrances.yaml"
    manifest.write_text(
        """
version: 1
universities:
  - name: TestU
    url: https://www.example.edu.cn/
    org_units:
      - name: 计算机学院
        faculty_urls:
          - https://cs.example.edu.cn/faculty
          - https://cs.example.edu.cn/faculty
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(EntranceManifestError, match="Duplicate URL"):
        load_university_entrance_targets(manifest)


def test_load_university_entrance_targets_rejects_org_unit_without_url(tmp_path):
    manifest = tmp_path / "websites.md"
    manifest.write_text(
        "\n".join(
            [
                "record_type,name,url,location,org_unit_name,org_unit_url,faculty_url",
                "university,TestU,https://www.example.edu.cn/,City,,,",
                "org_unit,TestU,,,CS,,",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(EntranceManifestError, match="requires org_unit_url or faculty_url"):
        load_university_entrance_targets(manifest)


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


async def test_ensure_runtime_schema_repairs_latin_name_with_cjk_alias_duplicate(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "schema_latin_cjk_alias_repair.db"))
    await db.init_db()

    async with db.session() as session:
        org_unit = OrgUnit(name="空间与地球科学学院", url="https://sse.buaa.edu.cn/", kind="college")
        session.add(org_unit)
        await session.flush()
        clean = Professor(
            name="Keizo Fujimoto",
            name_key="Keizo Fujimoto",
            org_unit_name="空间与地球科学学院",
            enrollment_pref="硕导",
            homepage="https://sse.buaa.edu.cn/info/1202/7126.htm",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        alias = Professor(
            name="Keizo Fujimoto (藤本桂三)",
            name_key="Keizo Fujimoto (藤本桂三)",
            org_unit_name="空间与地球科学学院",
            title="研究员",
            email="fujimoto@buaa.edu.cn",
            research_areas="空间物理；计算物理",
            bio="2017-present: 北京航空航天大学。",
            homepage="https://sse.buaa.edu.cn/info/1204/6490.htm",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add_all([clean, alias])
        await session.flush()
        session.add_all(
            [
                ProfessorAffiliation(
                    professor_id=clean.id,
                    org_unit_id=org_unit.id,
                    source_url="https://sse.buaa.edu.cn/szll/bssds.htm",
                    created_at=datetime.now(timezone.utc),
                ),
                ProfessorAffiliation(
                    professor_id=alias.id,
                    org_unit_id=org_unit.id,
                    source_url="https://sse.buaa.edu.cn/info/1204/6490.htm",
                    created_at=datetime.now(timezone.utc),
                ),
            ]
        )

    async with db.session() as session:
        await crawler_db.ensure_runtime_schema(session)

    async with db.session() as session:
        professor = (await session.execute(select(Professor))).scalar_one()
        affiliation = (await session.execute(select(ProfessorAffiliation))).scalar_one()
        assert professor.name == "Keizo Fujimoto"
        assert professor.name_key == "Keizo Fujimoto"
        assert professor.title == "研究员"
        assert professor.email == "fujimoto@buaa.edu.cn"
        assert professor.research_areas == "空间物理；计算物理"
        assert professor.bio == "2017-present: 北京航空航天大学。"
        assert professor.homepage == "https://sse.buaa.edu.cn/info/1204/6490.htm"
        assert affiliation.source_url == "https://sse.buaa.edu.cn/info/1204/6490.htm"

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


async def test_merge_sub_department_sections_moves_professors_to_parent_org_unit(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "sub_department_merge.db"))
    await db.init_db()

    async with db.session() as session:
        parent = await crawler_db.get_or_create_org_unit(
            session,
            name="自动化科学与电气工程学院",
            url="https://auto.example.edu.cn/",
            kind="college",
        )
        child = await crawler_db.get_or_create_org_unit(
            session,
            name="工业互联网与建模仿真系",
            url="https://auto.example.edu.cn/szdw/gongye.htm",
            kind="department",
        )
        child_professor = await crawler_db.upsert_professor(
            session,
            {
                "name": "Child Only",
                "title": "Professor",
                "org_unit_name": child.name,
                "org_unit_url": child.url,
                "source_url": "https://auto.example.edu.cn/szdw/gongye.htm",
            },
        )
        task = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name=child.name,
            org_unit_url=child.url,
            source_url="https://auto.example.edu.cn/szdw/gongye.htm",
            page_url="https://auto.example.edu.cn/szdw/gongye.htm",
            page_hash="child-page",
            page_text_snapshot="child",
            allowed_tools="save_professors",
        )
        await crawler_db.log_extraction_failure(
            session,
            task_id=int(task.id),
            failure_type="invalid_json",
            org_unit_name=child.name,
            source_url="https://auto.example.edu.cn/szdw/gongye.htm",
        )

        candidates = await crawler_db.list_sub_department_section_candidates(session)
        assert [candidate.child_name for candidate in candidates] == ["工业互联网与建模仿真系"]
        assert candidates[0].parent_name == "自动化科学与电气工程学院"

        summary = await crawler_db.merge_sub_department_sections(session, candidates)
        assert summary["sub_org_units_detected"] == 1
        assert summary["sub_org_units_merged"] == 1
        assert summary["affiliations_added"] == 1
        assert summary["affiliations_deleted"] == 1
        assert summary["crawl_tasks_rewritten"] == 1
        assert summary["crawl_extraction_failures_rewritten"] == 1

        parent_id = int(parent.id)
        professor_id = int(child_professor.id)

    async with db.session() as session:
        org_units = (await session.execute(select(OrgUnit).order_by(OrgUnit.name))).scalars().all()
        professors = (await session.execute(select(Professor).order_by(Professor.name))).scalars().all()
        affiliations = (await session.execute(select(ProfessorAffiliation))).scalars().all()
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()

        assert [unit.name for unit in org_units] == ["自动化科学与电气工程学院"]
        assert [(professor.name, professor.org_unit_name) for professor in professors] == [
            ("Child Only", "自动化科学与电气工程学院")
        ]
        assert [(aff.professor_id, aff.org_unit_id) for aff in affiliations] == [(professor_id, parent_id)]
        assert [(task.org_unit_name, task.org_unit_url) for task in tasks] == [
            ("自动化科学与电气工程学院", "https://auto.example.edu.cn")
        ]
        assert [(failure.task_id, failure.org_unit_name) for failure in failures] == [
            (tasks[0].id, "自动化科学与电气工程学院")
        ]

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


def test_normalize_professor_homepage_accepts_scu_query_detail_and_rejects_rosters():
    detail_url = "https://saa.scu.edu.cn/teamlist.htm?action=detailTeam&uuinId=661618903336854"

    assert normalize_professor_homepage(detail_url) == detail_url
    assert normalize_professor_homepage("https://saa.scu.edu.cn/teamlist.htm") is None
    assert normalize_professor_homepage("https://saa.scu.edu.cn/teamlist.htm?uuinUuteId=1761503632748932") is None
    assert normalize_professor_homepage("https://saa.scu.edu.cn/list.htm?m=1351479452361353") is None
    assert normalize_professor_homepage(
        "https://saa.scu.edu.cn/list.htm?m=1351479452361353&c=661618903336591&currentPage=1"
    ) is None


async def test_upsert_professor_promotes_scu_query_detail_homepage_from_roster(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "scu_query_detail_homepage.db"))
    await db.init_db()
    roster_url = "https://saa.scu.edu.cn/teamlist.htm?uuinUuteId=1761503632748933"
    detail_url = "https://saa.scu.edu.cn/teamlist.htm?action=detailTeam&uuinId=661618903336854"

    async with db.session() as session:
        await crawler_db.upsert_professor(
            session,
            {
                "name": "王靖宇",
                "org_unit_name": "空天科学与工程学院",
                "org_unit_url": "https://saa.scu.edu.cn/",
                "source_url": roster_url,
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "王靖宇",
                "org_unit_name": "空天科学与工程学院",
                "org_unit_url": "https://saa.scu.edu.cn/",
                "source_url": detail_url,
                "email": "wangjingyu@scu.edu.cn",
                "research_areas": "航空发动机旋转机械数值模拟方法研究",
            },
        )

    async with db.session() as session:
        professor = (await session.execute(select(Professor))).scalar_one()
        affiliation = (await session.execute(select(ProfessorAffiliation))).scalar_one()
        assert professor.homepage == detail_url
        assert professor.email == "wangjingyu@scu.edu.cn"
        assert affiliation.source_url == detail_url

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


async def test_upsert_professor_keeps_faculty_platform_as_external_link(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "faculty_platform_external.db"))
    await db.init_db()

    async with db.session() as session:
        await crawler_db.upsert_professor(
            session,
            {
                "name": "张三",
                "org_unit_name": "电子科学与工程学院",
                "org_unit_url": "https://www.ese.uestc.edu.cn/",
                "source_url": "https://www.ese.uestc.edu.cn/info/1001/1234.htm",
                "homepage": "https://faculty.uestc.edu.cn/zhangsan/zh_CN/index.htm",
            },
        )

    async with db.session() as session:
        professor = (await session.execute(select(Professor))).scalar_one()
        assert professor.homepage == "https://www.ese.uestc.edu.cn/info/1001/1234.htm"
        assert professor.external_link == "https://faculty.uestc.edu.cn/zhangsan/zh_CN/index.htm"

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


async def test_crawl_task_sanitizer_only_accepts_edu_cn_task_urls(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "task_edu_cn_sanitizer.db"))
    await db.init_db()

    assert crawler_db.is_edu_cn_task_url("https://edu.cn/faculty")
    assert crawler_db.is_edu_cn_task_url("https://cs.scu.edu.cn/szdw.htm")
    assert not crawler_db.is_edu_cn_task_url("https://www.example.com/faculty")
    assert not crawler_db.is_edu_cn_task_url("https://scu.edu.cn.evil.com/faculty")
    assert not crawler_db.is_edu_cn_task_url("/relative/faculty")

    async with db.session() as session:
        root_task = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://external.example.com/cs",
            source_url="https://edu.cn/faculty",
            page_url="https://edu.cn/faculty",
            page_hash="root",
            page_text_snapshot="faculty",
            allowed_tools='["save_professors"]',
        )
        subdomain_task = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://cs.scu.edu.cn/",
            source_url="https://cs.scu.edu.cn/szdw.htm",
            page_url="https://cs.scu.edu.cn/szdw.htm",
            page_hash="subdomain",
            page_text_snapshot="faculty",
            allowed_tools='["save_professors"]',
        )
        rejected_com = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://cs.scu.edu.cn/",
            source_url="https://www.example.com/faculty",
            page_url="https://www.example.com/faculty",
            page_hash="com",
            page_text_snapshot="faculty",
            allowed_tools='["save_professors"]',
        )
        rejected_evil = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://cs.scu.edu.cn/",
            source_url="https://scu.edu.cn.evil.com/faculty",
            page_url="https://scu.edu.cn.evil.com/faculty",
            page_hash="evil",
            page_text_snapshot="faculty",
            allowed_tools='["save_professors"]',
        )
        rejected_no_host = await crawler_db.upsert_crawl_task(
            session,
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://cs.scu.edu.cn/",
            source_url="/relative/faculty",
            page_url="/relative/faculty",
            page_hash="relative",
            page_text_snapshot="faculty",
            allowed_tools='["save_professors"]',
        )
        rows = (await session.execute(select(CrawlTask).order_by(CrawlTask.id))).scalars().all()

    assert root_task is not None
    assert root_task.org_unit_url is None
    assert subdomain_task is not None
    assert subdomain_task.org_unit_url == "https://cs.scu.edu.cn"
    assert rejected_com is None
    assert rejected_evil is None
    assert rejected_no_host is None
    assert [row.source_url for row in rows] == ["https://edu.cn/faculty", "https://cs.scu.edu.cn/szdw.htm"]
    await db.close()


async def test_cleanup_non_edu_cn_crawl_tasks_removes_invalid_tasks_and_org_unit_urls(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "task_edu_cn_cleanup.db"))
    await db.init_db()

    async with db.session() as session:
        valid = CrawlTask(
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://external.example.com/cs",
            source_url="https://cs.scu.edu.cn/szdw.htm",
            page_url="https://cs.scu.edu.cn/szdw.htm",
            page_hash="valid",
            task_kind=CrawlTaskKind.LIST_PAGE.value,
            page_text_snapshot="faculty",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING.value,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        invalid_source = CrawlTask(
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://cs.scu.edu.cn/",
            source_url="https://www.example.com/faculty",
            page_url="https://cs.scu.edu.cn/szdw.htm",
            page_hash="invalid-source",
            task_kind=CrawlTaskKind.LIST_PAGE.value,
            page_text_snapshot="faculty",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.PENDING.value,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        invalid_page = CrawlTask(
            university="TestU",
            org_unit_name="CS",
            org_unit_url="https://cs.scu.edu.cn/",
            source_url="https://cs.scu.edu.cn/detail.htm",
            page_url="https://scu.edu.cn.evil.com/detail.htm",
            page_hash="invalid-page",
            task_kind=CrawlTaskKind.DETAIL_PAGE.value,
            page_text_snapshot="faculty",
            allowed_tools='["save_professors"]',
            status=CrawlTaskStatus.RETRY.value,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        session.add_all([valid, invalid_source, invalid_page])
        await session.flush()
        await crawler_db.log_extraction_failure(
            session,
            task_id=int(invalid_source.id),
            failure_type="invalid_json",
            org_unit_name="CS",
            source_url=invalid_source.source_url,
        )

    async with db.session() as session:
        summary = await crawler_db.cleanup_non_edu_cn_crawl_tasks(session)
        rows = (await session.execute(select(CrawlTask))).scalars().all()
        failures = (await session.execute(select(CrawlExtractionFailure))).scalars().all()

    assert summary["crawl_tasks_scanned"] == 3
    assert summary["crawl_tasks_deleted"] == 2
    assert summary["crawl_extraction_failures_deleted"] == 1
    assert summary["crawl_task_org_unit_urls_cleared"] == 1
    assert len(rows) == 1
    assert rows[0].source_url == "https://cs.scu.edu.cn/szdw.htm"
    assert rows[0].org_unit_url is None
    assert failures == []
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


async def test_crawl_graph_node_and_edge_upsert_dedupes_and_merges_metadata(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "crawl_graph.db"))
    await db.init_db()

    async with db.session() as session:
        source = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/faculty#top",
            org_unit_name="CS",
            org_unit_id=1,
            priority_score=80,
            metadata={"source": "list"},
        )
        same_source = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/faculty",
            org_unit_name="Computer Science",
            org_unit_id=1,
            priority_score=85,
            metadata={"seen_on": ["home"]},
        )
        other_org_source = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/faculty",
            org_unit_name="AI",
            org_unit_id=2,
            priority_score=80,
        )
        detail = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.DETAIL_URL,
            url="https://cs.example.edu.cn/info/1001/ada.htm",
            org_unit_name="CS",
            org_unit_id=1,
            priority_score=40,
        )
        edge = await crawler_db.upsert_graph_edge(
            session,
            from_node_id=source.id,
            to_node_id=detail.id,
            edge_type=CrawlGraphEdgeType.DETAIL_CANDIDATE_OF,
            metadata={"page": 1},
        )
        same_edge = await crawler_db.upsert_graph_edge(
            session,
            from_node_id=same_source.id,
            to_node_id=detail.id,
            edge_type=CrawlGraphEdgeType.DETAIL_CANDIDATE_OF,
            metadata={"page": 2},
        )
        nodes = (await session.execute(select(CrawlGraphNode))).scalars().all()
        edges = (await session.execute(select(CrawlGraphEdge))).scalars().all()

    assert same_source.id == source.id
    assert edge.id == same_edge.id
    assert other_org_source.id != source.id
    assert len(nodes) == 3
    assert len(edges) == 1
    assert same_source.priority_score == 85
    assert same_source.org_unit_name == "Computer Science"
    assert same_source.org_unit_id == 1
    metadata = json.loads(same_source.metadata_json)
    assert metadata == {"discovery_source": "list"}
    await db.close()


async def test_crawl_graph_ready_nodes_sort_by_priority_and_status(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "crawl_graph_ready.db"))
    await db.init_db()

    async with db.session() as session:
        low = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.DETAIL_URL,
            url="https://cs.example.edu.cn/info/low.htm",
            org_unit_name="CS",
            org_unit_id=1,
            priority_score=40,
            depth=3,
        )
        high = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/faculty",
            org_unit_name="CS",
            org_unit_id=1,
            priority_score=80,
            depth=1,
        )
        await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_FOLLOWUP_URL,
            url="https://cs.example.edu.cn/faculty/archive",
            org_unit_name="CS",
            org_unit_id=1,
            status=CrawlGraphNodeStatus.DONE,
            priority_score=90,
        )
        retry = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.PAGINATION_URL,
            url="https://cs.example.edu.cn/faculty/2.htm",
            org_unit_name="CS",
            org_unit_id=1,
            status=CrawlGraphNodeStatus.RETRY,
            priority_score=70,
        )
        other_org = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://ai.example.edu.cn/faculty",
            org_unit_name="AI",
            org_unit_id=2,
            priority_score=100,
        )
        rows = await crawler_db.list_ready_graph_nodes(session, limit=10)
        org_rows = await crawler_db.list_ready_graph_nodes(session, limit=10, org_unit_ids=[1])
        await crawler_db.mark_graph_node_status(
            session,
            low.id,
            status=CrawlGraphNodeStatus.FAILED,
            last_error="fetch_failed",
            increment_attempt=True,
        )
        failed = await session.get(CrawlGraphNode, low.id)

    assert [row.id for row in rows] == [other_org.id, high.id, retry.id, low.id]
    assert [row.id for row in org_rows] == [high.id, retry.id, low.id]
    assert failed.status == CrawlGraphNodeStatus.FAILED.value
    assert failed.attempt_count == 1
    assert failed.priority_score == 35
    assert failed.last_error == "fetch_failed"
    await db.close()


async def test_ensure_runtime_schema_creates_crawl_graph_tables_for_existing_db(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "crawl_graph_schema.db"))
    await db.init_db()

    async with db.session() as session:
        await session.execute(text("DROP TABLE crawl_graph_edges"))
        await session.execute(text("DROP TABLE crawl_graph_nodes"))

    async with db.session() as session:
        await crawler_db.ensure_runtime_schema(session)
        tables = (
            await session.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name IN ('crawl_graph_nodes', 'crawl_graph_edges')"
                )
            )
        ).scalars().all()
        await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.ORG_LISTING_URL,
            url="https://www.example.edu.cn/orgs",
        )

    assert set(tables) == {"crawl_graph_nodes", "crawl_graph_edges"}
    await db.close()


async def test_graph_node_tracks_base_priority_raise_only(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "graph_base_priority.db"))
    await db.init_db()
    async with db.session() as session:
        node = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/faculty",
            org_unit_name="CS",
            priority_score=80,
        )
        assert node.base_priority == 80.0

        # Higher re-discovery raises base_priority.
        raised = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/faculty",
            org_unit_name="CS",
            priority_score=95,
        )
        assert raised.id == node.id
        assert raised.base_priority == 95.0

        # Lower re-discovery does NOT lower base_priority.
        lowered = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/faculty",
            org_unit_name="CS",
            priority_score=10,
        )
        assert lowered.base_priority == 95.0
    await db.close()


async def test_graph_node_rediscovery_reopens_failed_but_keeps_done_sticky(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "graph_reopen.db"))
    await db.init_db()
    async with db.session() as session:
        failed = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.DETAIL_URL,
            url="https://cs.example.edu.cn/info/x.htm",
            org_unit_name="CS",
            status=CrawlGraphNodeStatus.FAILED,
        )
        assert failed.status == CrawlGraphNodeStatus.FAILED.value

        reopened = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.DETAIL_URL,
            url="https://cs.example.edu.cn/info/x.htm",
            org_unit_name="CS",
            status=CrawlGraphNodeStatus.PENDING,
        )
        assert reopened.id == failed.id
        assert reopened.status == CrawlGraphNodeStatus.RETRY.value

        done = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.DETAIL_URL,
            url="https://cs.example.edu.cn/info/y.htm",
            org_unit_name="CS",
            status=CrawlGraphNodeStatus.DONE,
        )
        still_done = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.DETAIL_URL,
            url="https://cs.example.edu.cn/info/y.htm",
            org_unit_name="CS",
            status=CrawlGraphNodeStatus.PENDING,
        )
        assert still_done.id == done.id
        assert still_done.status == CrawlGraphNodeStatus.DONE.value
    await db.close()


async def test_recover_stale_in_progress_graph_nodes_resets_only_in_progress(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "graph_recover.db"))
    await db.init_db()
    async with db.session() as session:
        stale = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/faculty",
            org_unit_name="CS",
            status=CrawlGraphNodeStatus.IN_PROGRESS,
        )
        pending = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/faculty/2.htm",
            org_unit_name="CS",
            status=CrawlGraphNodeStatus.PENDING,
        )
        done = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.DETAIL_URL,
            url="https://cs.example.edu.cn/info/x.htm",
            org_unit_name="CS",
            status=CrawlGraphNodeStatus.DONE,
        )

        recovered = await crawler_db.recover_stale_in_progress_graph_nodes(session)
        assert recovered == 1

        stale_row = await session.get(CrawlGraphNode, stale.id)
        pending_row = await session.get(CrawlGraphNode, pending.id)
        done_row = await session.get(CrawlGraphNode, done.id)
        assert stale_row.status == CrawlGraphNodeStatus.RETRY.value
        assert stale_row.last_error == "recovered_stale_in_progress"
        assert pending_row.status == CrawlGraphNodeStatus.PENDING.value
        assert done_row.status == CrawlGraphNodeStatus.DONE.value
    await db.close()
