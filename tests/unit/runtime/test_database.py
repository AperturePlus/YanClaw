from __future__ import annotations

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
    await db.close()

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == 15000
    assert int(synchronous) == 1  # 1 == NORMAL
