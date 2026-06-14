from __future__ import annotations

from sqlalchemy import text

from runtime.database import DatabaseManager
from tests.conftest import sqlite_url


async def test_init_db_creates_database_file(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "runtime.db"))
    await db.init_db()

    assert (tmp_path / "runtime.db").exists()

    await db.close()


async def test_sqlite_connection_uses_wal_and_busy_timeout(tmp_path):
    """Concurrency PRAGMAs must be applied so concurrent writers don't hit
    'database is locked' immediately."""
    db = DatabaseManager(sqlite_url(tmp_path / "runtime.db"), busy_timeout_ms=12345)
    await db.init_db()

    async with db.session() as session:
        journal_mode = (await session.execute(text("PRAGMA journal_mode"))).scalar()
        busy_timeout = (await session.execute(text("PRAGMA busy_timeout"))).scalar()
        synchronous = (await session.execute(text("PRAGMA synchronous"))).scalar()

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == 12345
    assert int(synchronous) == 1  # NORMAL

    await db.close()


async def test_database_manager_creates_sqlite_parent_directory(tmp_path):
    database_path = tmp_path / "nested" / "runtime.db"
    db = DatabaseManager(sqlite_url(database_path))
    await db.init_db()

    assert database_path.exists()

    await db.close()
