from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class DatabaseManager:
    """Async SQLAlchemy engine and session factory."""

    _model_modules: list[str] = []

    @classmethod
    def register_models(cls, module_path: str) -> None:
        """Register a module containing ORM models to be imported during init_db."""
        if module_path not in cls._model_modules:
            cls._model_modules.append(module_path)

    def __init__(
        self,
        database_url: str,
        *,
        echo: bool = False,
        busy_timeout_ms: int = 30000,
    ) -> None:
        self.database_url = database_url
        _ensure_sqlite_parent_dir(database_url)
        self.engine: AsyncEngine = create_async_engine(database_url, echo=echo)
        if make_url(database_url).drivername.startswith("sqlite"):
            _install_sqlite_pragmas(self.engine, busy_timeout_ms)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        session = self.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def init_db(self) -> None:
        import importlib

        for module_path in self._model_modules:
            try:
                importlib.import_module(module_path)
            except ImportError:
                pass

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()


def _install_sqlite_pragmas(engine: AsyncEngine, busy_timeout_ms: int) -> None:
    """Apply concurrency-friendly PRAGMAs to every new SQLite connection.

    Each university uses one SQLite file written by several async connections
    (the extraction pipeline plus the page-cache writer). The default
    rollback-journal mode makes readers and writers block each other, so
    concurrent writes raise ``sqlite3.OperationalError: database is locked``
    immediately. WAL lets one writer and many readers proceed without
    blocking, and ``busy_timeout`` makes a writer wait for the lock instead of
    failing outright. PRAGMAs are connection-scoped (journal_mode persists on
    the file, the rest do not), so they must be set on every connect.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        finally:
            cursor.close()


def _ensure_sqlite_parent_dir(database_url: str) -> None:
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return

    database = url.database
    if not database or database == ":memory:":
        return

    path = Path(database).expanduser()
    parent = path.parent
    if str(parent) not in {"", "."}:
        parent.mkdir(parents=True, exist_ok=True)
