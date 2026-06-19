# Graph Crawler — Phase 0: WAL/SQLite Concurrency Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Configure the per-university SQLite async engine with WAL + `busy_timeout` + `synchronous=NORMAL` (and, gated, `foreign_keys=ON`) so concurrent writers (the fetch driver + LLM/DB worker pool) stop risking `database is locked`.

**Architecture:** Attach a SQLAlchemy `connect` event listener to `DatabaseManager.engine.sync_engine` that runs the PRAGMAs on every new DBAPI connection. `journal_mode=WAL` persists in the DB file header; `busy_timeout`/`synchronous`/`foreign_keys` are per-connection and re-applied on each connect. Guarded to the `sqlite` dialect.

**Tech Stack:** Python, SQLAlchemy 2.0 async, aiosqlite, pytest + pytest-asyncio (`asyncio_mode=auto`), `uv` runner.

**Source spec:** `docs/superpowers/specs/2026-06-10-graph-crawler-migration-design.md` (§4.5). This is Phase 0 of the phased delivery; transaction batching (also in §4.5) is deferred to the phases that rewrite the write paths.

---

### Task 1: Apply WAL / busy_timeout / synchronous PRAGMAs on connect

**Files:**
- Modify: `src/runtime/database.py` (imports; `DatabaseManager.__init__`; new module-level helper)
- Test: `tests/unit/runtime/test_database.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/runtime/test_database.py`:

```python
from sqlalchemy import text


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_database.py::test_sqlite_engine_enables_wal_and_tuned_pragmas -v`
Expected: FAIL — `journal_mode` is `delete` (asserts `wal`) and `synchronous` is `2` (asserts `1`).

- [ ] **Step 3: Add the `event` import**

In `src/runtime/database.py`, change the SQLAlchemy import block. Current top imports are:

```python
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
```

Add one line above `from sqlalchemy.engine import make_url`:

```python
from sqlalchemy import event
from sqlalchemy.engine import make_url
```

- [ ] **Step 4: Add the pragma helper and call it in `__init__`**

In `src/runtime/database.py`, `DatabaseManager.__init__` currently reads:

```python
        self.database_url = database_url
        _ensure_sqlite_parent_dir(database_url)
        self.engine: AsyncEngine = create_async_engine(database_url, echo=echo)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
```

Insert the helper call right after the engine is created:

```python
        self.database_url = database_url
        _ensure_sqlite_parent_dir(database_url)
        self.engine: AsyncEngine = create_async_engine(database_url, echo=echo)
        _configure_sqlite_engine(self.engine)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
```

Add this module-level helper immediately above `def _ensure_sqlite_parent_dir(database_url: str) -> None:`:

```python
def _configure_sqlite_engine(engine: AsyncEngine) -> None:
    """Apply concurrency/durability PRAGMAs to every new SQLite connection.

    journal_mode=WAL is persisted in the database header; busy_timeout and
    synchronous are per-connection and must be re-applied on each connect.
    """
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/unit/runtime/test_database.py::test_sqlite_engine_enables_wal_and_tuned_pragmas -v`
Expected: PASS

- [ ] **Step 6: Run the existing runtime DB tests to confirm no regression**

Run: `uv run pytest tests/unit/runtime/test_database.py -v`
Expected: PASS (3 tests: the 2 existing + the new one).

- [ ] **Step 7: Commit**

```bash
git add src/runtime/database.py tests/unit/runtime/test_database.py
git commit -m "perf(db): enable WAL/busy_timeout/synchronous on sqlite connections"
```

---

### Task 2: Concurrency regression guard (no deadlock under parallel writers)

This is a **guard test** — it documents and protects the behavior Task 1 unlocks. It is expected to PASS after Task 1 (not a fail-first TDD test).

**Files:**
- Test: `tests/unit/runtime/test_database.py`

- [ ] **Step 1: Add the concurrency test**

Add to `tests/unit/runtime/test_database.py` (the `text` import was added in Task 1; add `import asyncio` at the top of the file if not present):

```python
import asyncio


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
```

- [ ] **Step 2: Run the test to verify it passes**

Run: `uv run pytest tests/unit/runtime/test_database.py::test_concurrent_writers_do_not_lock -v`
Expected: PASS (160 rows, no `OperationalError: database is locked`).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/runtime/test_database.py
git commit -m "test(db): guard concurrent sqlite writers against locking"
```

---

### Task 3: Enable `foreign_keys=ON` (gated — full-suite verified, revertible)

`foreign_keys=ON` turns on enforcement that was previously OFF, so it can surface latent FK-ordering bugs in raw-SQL deletes (e.g. `cleanup_non_edu_cn_crawl_tasks`). This task adds it and proves the existing crawler/steward suites stay green; if a real integrity issue surfaces, that is a genuine latent bug to fix separately — revert this task and proceed without it rather than masking it.

**Files:**
- Modify: `src/runtime/database.py` (the pragma listener)
- Test: `tests/unit/runtime/test_database.py`

- [ ] **Step 1: Extend the pragma test to assert foreign_keys is ON**

Edit `test_sqlite_engine_enables_wal_and_tuned_pragmas` in `tests/unit/runtime/test_database.py` to also read and assert `foreign_keys`. The body becomes:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_database.py::test_sqlite_engine_enables_wal_and_tuned_pragmas -v`
Expected: FAIL — `foreign_keys` is `0` (asserts `1`).

- [ ] **Step 3: Add `foreign_keys=ON` to the listener**

In `src/runtime/database.py`, in `_set_sqlite_pragmas`, add the `foreign_keys` PRAGMA after `synchronous`:

```python
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=15000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/runtime/test_database.py::test_sqlite_engine_enables_wal_and_tuned_pragmas -v`
Expected: PASS

- [ ] **Step 5: Run the full crawler + steward + runtime suites (the gate)**

Run: `uv run pytest tests/unit/crawler tests/unit/steward tests/unit/runtime -q`
Expected: PASS. If any test now fails with a foreign-key constraint error, that is a latent integrity bug exposed by enforcement — STOP, report it, and (unless it is trivially fixable in the failing delete-ordering) revert Step 3 so Phase 0 ships the WAL/busy_timeout/synchronous wins without FK enforcement.

- [ ] **Step 6: Commit**

```bash
git add src/runtime/database.py tests/unit/runtime/test_database.py
git commit -m "fix(db): enforce sqlite foreign_keys=ON"
```

---

## Self-Review

- **Spec coverage (§4.5):** WAL ✅ (Task 1), busy_timeout ✅ (Task 1), synchronous=NORMAL ✅ (Task 1), foreign_keys=ON ✅ (Task 3, gated). Transaction batching is explicitly deferred to the write-path phases — noted in the header, not a Phase 0 gap.
- **Placeholders:** none — every code/command step shows the real diff and the exact `uv run pytest` invocation with expected output.
- **Type/name consistency:** the helper `_configure_sqlite_engine` and listener `_set_sqlite_pragmas` are referenced consistently; the test name `test_sqlite_engine_enables_wal_and_tuned_pragmas` is edited (not duplicated) in Task 3.
- **Ships green:** after Task 3 (or its revert), `tests/unit/runtime/test_database.py` and the crawler/steward suites pass — Phase 0 is independently shippable.
