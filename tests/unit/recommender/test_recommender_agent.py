from __future__ import annotations

import asyncio
import json
from pathlib import Path

from click.testing import CliRunner

from agents.crawler import db as crawler_db
from agents.crawler.config import CrawlerSettings
from agents.recommender.agent import RecommendationAgent
from agents.recommender.db import repository
from agents.recommender.graph_agent import KnowledgeGraphAgent
from runtime.crawler_cli import cli
from runtime.database import DatabaseManager
from tests.conftest import sqlite_url


async def _seed_university_db(db_path: Path) -> None:
    db = DatabaseManager(sqlite_url(db_path))
    await db.init_db()
    async with db.session() as session:
        await crawler_db.ensure_runtime_schema(session)
        await crawler_db.ensure_university_meta(
            session,
            name="TestU",
            start_url="https://www.example.edu.cn/",
            location="北京",
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Ada",
                "org_unit_name": "计算机学院",
                "org_unit_url": "https://cs.example.edu.cn/",
                "title": "教授",
                "research_areas": "人工智能；机器学习；自然语言处理",
                "bio": "长期从事智能系统与大模型研究。",
                "enrollment_pref": "博士生导师",
                "publications": "机器学习与自然语言处理代表作",
                "email": "ada@example.edu.cn",
                "source_url": "https://cs.example.edu.cn/info/1001/1.htm",
            },
        )
        await crawler_db.upsert_professor(
            session,
            {
                "name": "Bob",
                "org_unit_name": "材料学院",
                "org_unit_url": "https://mat.example.edu.cn/",
                "title": "副教授",
                "research_areas": "量子材料；能源材料",
                "bio": "从事材料物理研究。",
                "enrollment_pref": "硕士生导师",
                "source_url": "https://mat.example.edu.cn/info/1001/2.htm",
            },
        )
    await db.close()


def _settings(tmp_path: Path) -> CrawlerSettings:
    websites = tmp_path / "websites.csv"
    websites.write_text(
        "name,url,location\nTestU,https://www.example.edu.cn/,北京\n",
        encoding="utf-8",
    )
    db_dir = tmp_path / "universities"
    db_dir.mkdir(parents=True, exist_ok=True)
    return CrawlerSettings(
        websites_path=websites,
        university_db_dir=db_dir,
        knowledge_graph_db_path=tmp_path / "knowledge_graph.db",
        openai_api_key="",
    )


async def test_graph_build_creates_nodes_edges_and_terms(tmp_path):
    settings = _settings(tmp_path)
    await _seed_university_db(Path(settings.university_db_dir) / "example.edu.cn.db")

    summary = await KnowledgeGraphAgent(settings=settings).build(
        universities=["TestU"],
        rebuild=True,
    )

    assert summary.source_count == 1
    assert summary.indexed_sources == 1
    assert summary.nodes_written > 0
    assert summary.edges_written > 0
    assert summary.terms_written > 0
    async with repository.connect_graph(settings.knowledge_graph_db_path) as conn:
        assert await repository.count_rows(conn, "kg_nodes") >= 5
        assert await repository.count_rows(conn, "kg_edges") >= 4
        assert await repository.count_rows(conn, "kg_terms") > 0


async def test_graph_build_skips_unchanged_sources(tmp_path):
    settings = _settings(tmp_path)
    await _seed_university_db(Path(settings.university_db_dir) / "example.edu.cn.db")
    agent = KnowledgeGraphAgent(settings=settings)

    first = await agent.build(universities=["TestU"], rebuild=True)
    second = await agent.build(universities=["TestU"], rebuild=False)

    assert first.indexed_sources == 1
    assert second.indexed_sources == 0
    assert second.skipped_sources == 1


async def test_recommendation_uses_local_profile_parser_without_api_key(tmp_path):
    settings = _settings(tmp_path)
    await _seed_university_db(Path(settings.university_db_dir) / "example.edu.cn.db")
    await KnowledgeGraphAgent(settings=settings).build(universities=["TestU"], rebuild=True)

    result = await RecommendationAgent(settings=settings).recommend(
        text="我想申请北京的人工智能和机器学习方向博士，希望找博士生导师。",
        top_schools=3,
        top_org_units=3,
        top_professors=3,
        auto_build=False,
    )

    assert result.professors
    assert result.professors[0].name == "Ada"
    assert result.professors[0].score > 0
    assert result.org_units[0].org_unit_name == "计算机学院"
    assert result.schools[0].university_name == "TestU"
    assert "人工智能" in result.profile.raw_text


def test_cli_graph_build_and_recommend_json_text_and_file(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    asyncio.run(_seed_university_db(Path(settings.university_db_dir) / "example.edu.cn.db"))
    monkeypatch.setenv("YANCLAW_WEBSITES_PATH", str(settings.websites_path))
    monkeypatch.setenv("YANCLAW_UNIVERSITY_DB_DIR", str(settings.university_db_dir))
    monkeypatch.setenv("YANCLAW_KNOWLEDGE_GRAPH_DB_PATH", str(settings.knowledge_graph_db_path))
    monkeypatch.setenv("YANCLAW_OPENAI_API_KEY", "")

    runner = CliRunner()
    build_result = runner.invoke(cli, ["graph", "build", "--universities", "TestU", "--rebuild"])
    assert build_result.exit_code == 0, build_result.output
    assert "indexed=1" in build_result.output

    recommend_result = runner.invoke(
        cli,
        [
            "recommend",
            "--text",
            "人工智能 机器学习 博士 北京",
            "--top-professors",
            "2",
            "--json",
            "--no-auto-build",
        ],
    )
    assert recommend_result.exit_code == 0, recommend_result.output
    parsed = json.loads(recommend_result.output)
    assert parsed["professors"][0]["name"] == "Ada"

    resume = tmp_path / "resume.txt"
    resume.write_text("研究兴趣：人工智能、自然语言处理。目标：博士。", encoding="utf-8")
    file_result = runner.invoke(
        cli,
        [
            "recommend",
            "--file",
            str(resume),
            "--top-professors",
            "2",
            "--json",
            "--no-auto-build",
        ],
    )
    assert file_result.exit_code == 0, file_result.output
    parsed_file = json.loads(file_result.output)
    assert parsed_file["professors"][0]["name"] == "Ada"
