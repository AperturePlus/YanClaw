from __future__ import annotations

import asyncio

from sqlalchemy import text

from runtime.database import DatabaseManager
from tests.conftest import sqlite_url


async def test_init_db_creates_database_file(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "runtime.db"))
    await db.init_db()

    assert (tmp_path / "runtime.db").exists()

    await db.close()


async def test_database_manager_creates_sqlite_parent_directory(tmp_path):
    database_path = tmp_path / "nested" / "runtime.db"
    db = DatabaseManager(sqlite_url(database_path))
    await db.init_db()

    assert database_path.exists()

    await db.close()


async def test_sqlite_engine_enables_wal_and_tuned_pragmas(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "wal.db"))
    await db.init_db()
    async with db.session() as session:
        journal_mode = (await session.execute(text("PRAGMA journal_mode"))).scalar()
        busy_timeout = (await session.execute(text("PRAGMA busy_timeout"))).scalar()
        synchronous = (await session.execute(text("PRAGMA synchronous"))).scalar()
        foreign_keys = (await session.execute(text("PRAGMA foreign_keys"))).scalar()
    await db.close()

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == 15000
    assert int(synchronous) == 1  # 1 == NORMAL
    assert int(foreign_keys) == 1  # 1 == ON


async def test_concurrent_writers_do_not_lock(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "concurrent.db"))
    await db.init_db()
    async with db.session() as session:
        await session.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, v INTEGER)"))

    async def writer(worker: int) -> None:
        for i in range(20):
            async with db.session() as session:
                await session.execute(
                    text("INSERT INTO t (v) VALUES (:v)"),
                    {"v": worker * 100 + i},
                )

    await asyncio.gather(*(writer(w) for w in range(8)))

    async with db.session() as session:
        total = (await session.execute(text("SELECT COUNT(*) FROM t"))).scalar()
    await db.close()

    assert int(total) == 160
