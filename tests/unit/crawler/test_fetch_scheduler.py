from __future__ import annotations

from sqlalchemy import select

from agents.crawler import db as crawler_db
from agents.crawler.fetch_scheduler import FetchScheduler
from agents.crawler.fetchers import FetchResult
from agents.crawler.models import CrawlLog
from runtime.database import DatabaseManager
from tests.conftest import sqlite_url


class _Logger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.debugs: list[str] = []

    def warning(self, message: str, *args: object) -> None:
        self.warnings.append(message % args if args else message)

    def info(self, message: str, *args: object) -> None:
        self.infos.append(message % args if args else message)

    def debug(self, message: str, *args: object) -> None:
        self.debugs.append(message % args if args else message)


class _Fetcher:
    def __init__(self, result: FetchResult) -> None:
        self.result = result

    async def fetch(self, url: str, **_kwargs: object) -> FetchResult:
        return self.result


class _Agent:
    def __init__(self, db: DatabaseManager, result: FetchResult) -> None:
        self.db = db
        self.start_url = "https://www.example.edu.cn/"
        self._skip_cross_run_dedup = True
        self._resume_force_refetch_urls: set[str] = set()
        self.visited_urls: set[str] = set()
        self._fetch_cache: dict[str, FetchResult] = {}
        self.execution_log: list[str] = []
        self.logger = _Logger()
        self.max_depth = 3
        self.fetcher = _Fetcher(result)
        self._blocked_hosts: set[str] = set()

    def _within_depth(self, depth: int) -> bool:
        return depth <= self.max_depth


async def test_timeout_fetch_failure_does_not_mark_host_blocked(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "timeout_fetch.db"))
    await db.init_db()
    result = FetchResult(
        "https://www.example.edu.cn/szdw.html",
        "",
        [],
        0,
        block_reason="timeout",
    )
    agent = _Agent(db, result)

    fetched = await FetchScheduler(agent).fetch_url("https://www.example.edu.cn/szdw.html", 1)

    assert fetched is result
    assert agent._blocked_hosts == set()
    assert any("Human fetch timed out" in item for item in agent.logger.warnings)
    assert any("fetch retryable_failure" in item for item in agent.execution_log)

    async with db.session() as session:
        logs = (await session.execute(select(CrawlLog).order_by(CrawlLog.id))).scalars().all()
    assert logs[-1].message == "depth=1 status_code=0 fetch_failure=timeout links=0"
    await db.close()


async def test_waf_fetch_failure_marks_host_blocked(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "waf_fetch.db"))
    await db.init_db()
    result = FetchResult(
        "https://www.example.edu.cn/szdw.html",
        "",
        [],
        403,
        block_reason="waf_challenge status=403 markers=challenge",
    )
    agent = _Agent(db, result)

    fetched = await FetchScheduler(agent).fetch_url("https://www.example.edu.cn/szdw.html", 1)

    assert fetched is result
    assert agent._blocked_hosts == {"www.example.edu.cn"}
    assert any("WAF/challenge page detected" in item for item in agent.logger.warnings)
    assert any("fetch blocked" in item for item in agent.execution_log)

    async with db.session() as session:
        logs = (await session.execute(select(CrawlLog).order_by(CrawlLog.id))).scalars().all()
    assert logs[-1].message == (
        "depth=1 status_code=403 blocked=waf_challenge status=403 markers=challenge links=0"
    )
    await db.close()


async def test_retryable_fetch_failure_query_accepts_new_timeout_message(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "retryable_message.db"))
    await db.init_db()

    async with db.session() as session:
        await crawler_db.log_crawl(
            session,
            "https://www.example.edu.cn/szdw.html",
            "failed",
            "depth=1 status_code=0 fetch_failure=timeout links=0",
        )
        urls = await crawler_db.list_retryable_fetch_failure_urls(session)

    assert urls == ["https://www.example.edu.cn/szdw.html"]
    await db.close()
