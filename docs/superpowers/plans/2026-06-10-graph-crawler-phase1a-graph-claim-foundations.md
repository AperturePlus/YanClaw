# Graph Crawler — Phase 1a: Graph-Claim DB Foundations (B1/B2/B3 + atomic claim) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the graph persistence-layer primitives the claim-driven crawler will run on — an authoritative `base_priority`, an atomic single-row claim (`claim_next_graph_node`), stale-`IN_PROGRESS` recovery (`recover_stale_in_progress_graph_nodes`), and a re-discovery rule that re-opens non-`DONE` terminal nodes — and land bug fixes **B1, B2, B3** at the DB layer. This is purely additive: no crawler control-flow changes yet, so the system keeps running exactly as today and every suite stays green.

**Architecture:** All graph state lives in `crawl_graph_nodes` (`src/agents/crawler/db/graph.py`, ORM in `models.py`, raw-SQL migration in `db/schema.py`). Today `list_ready_graph_nodes` only *reads* ready nodes (no atomic claim) and re-discovery raises `priority_score` while attempt-backoff lowers it — so re-discovery erases backoff (B3); terminal `DONE` and `FAILED` share a rank so a re-discovered `FAILED` node can never re-open (B2); and there is no analogue of `recover_stale_in_progress_crawl_tasks` for nodes (B1). This phase stores an intrinsic `base_priority` separate from the mutable `priority_score`, computes an **effective priority** (`base_priority − attempt_count × penalty`) only at claim time, makes claiming a single select-then-flip transaction (one driver → atomic by construction), adds node-level stale recovery, and lets re-discovery lift a non-`DONE` terminal node to `RETRY`.

**Tech Stack:** Python, SQLAlchemy 2.0 async, aiosqlite, pytest + pytest-asyncio (`asyncio_mode=auto`), `uv` runner.

**Source spec:** `docs/superpowers/specs/2026-06-10-graph-crawler-migration-design.md` — §4.1 (node model/lifecycle), §4.2 (claim/consume loop), §4.4 (recovery), §5 (B1, B2, B3), §6 (testing). This is the **first half of spec Phase 1**; the claim-driver rewrite, worker terminal-status ownership, detail-flow refactor, WAF single-fetch invariant test, and B4/B5/B6 are **Phase 1b** (planned separately, on top of these foundations).

**Baseline note (read before starting):** The working tree already contains **uncommitted** "list pages = traversal-only" changes (`extraction_pipeline.py`, `extraction_payloads.py`, `agent.py`, `agent_detail.py`, `prompt_builder.py`, `session_state.py`, `skills/save-professors.md`, and their tests). That uncommitted state is the intended Phase 1 baseline — **do not revert it**. This plan does **not** touch those files; each task below uses a *targeted* `git add` of only the graph/DB files it changes, so the traversal-only work stays uncommitted (it is committed during Phase 1b, which edits those files). The full crawler suite is green with the uncommitted work in place.

---

### Task 1: Store an intrinsic `base_priority` on every graph node (B3 storage)

`base_priority` is the node's intrinsic ranking value (from type/url/source). Unlike `priority_score`, it is never lowered by attempt-backoff, so the claim step (Task 4) can subtract a backoff penalty without re-discovery undoing it. We add the column to the ORM, to the raw-SQL migration (for existing production DBs), and set/maintain it in `upsert_graph_node`.

**Files:**
- Modify: `src/agents/crawler/models.py` (`CrawlGraphNode`, after `priority_score`)
- Modify: `src/agents/crawler/db/schema.py` (`_ensure_crawl_graph_schema`: CREATE TABLE DDL + gated ALTER)
- Modify: `src/agents/crawler/db/graph.py` (`upsert_graph_node`: insert + raise-only update)
- Test: `tests/unit/crawler/test_db.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/crawler/test_db.py` (all imports — `crawler_db`, `CrawlGraphNode`, `CrawlGraphNodeType`, `DatabaseManager`, `sqlite_url` — already exist in this file):

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/crawler/test_db.py::test_graph_node_tracks_base_priority_raise_only -v`
Expected: FAIL — `AttributeError: 'CrawlGraphNode' object has no attribute 'base_priority'`.

- [ ] **Step 3: Add the `base_priority` ORM column**

In `src/agents/crawler/models.py`, in `class CrawlGraphNode`, the priority line currently reads:

```python
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
```

Add `base_priority` immediately after it:

```python
    priority_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    base_priority: Mapped[float] = mapped_column(Float, default=0.0)
```

- [ ] **Step 4: Add `base_priority` to the raw-SQL migration**

In `src/agents/crawler/db/schema.py`, in `_ensure_crawl_graph_schema`, the `CREATE TABLE IF NOT EXISTS crawl_graph_nodes` block has this line:

```python
                priority_score FLOAT NOT NULL DEFAULT 0.0,
```

Add a `base_priority` line right after it:

```python
                priority_score FLOAT NOT NULL DEFAULT 0.0,
                base_priority FLOAT NOT NULL DEFAULT 0.0,
```

Then, right after the existing `org_unit_id` migration block (the `if not await _sqlite_has_column(session, "crawl_graph_nodes", "org_unit_id"):` block and its index), add a gated migration that adds the column to pre-existing DBs and backfills it from `priority_score`:

```python
    if not await _sqlite_has_column(session, "crawl_graph_nodes", "base_priority"):
        await session.execute(
            text("ALTER TABLE crawl_graph_nodes ADD COLUMN base_priority FLOAT NOT NULL DEFAULT 0.0")
        )
        await session.execute(
            text("UPDATE crawl_graph_nodes SET base_priority = priority_score WHERE base_priority = 0.0")
        )
```

- [ ] **Step 5: Set & maintain `base_priority` in `upsert_graph_node`**

In `src/agents/crawler/db/graph.py`, in `upsert_graph_node`, the insert path constructs the row. Add `base_priority` to the `CrawlGraphNode(...)` constructor right after the `priority_score=...` line:

```python
            priority_score=float(priority_score or 0.0),
            base_priority=float(priority_score or 0.0),
```

In the update path, the existing priority-raise block reads:

```python
    if float(priority_score or 0.0) > float(row.priority_score or 0.0):
        row.priority_score = float(priority_score or 0.0)
        changed = True
```

Add a parallel raise-only block for `base_priority` immediately after it:

```python
    if float(priority_score or 0.0) > float(row.priority_score or 0.0):
        row.priority_score = float(priority_score or 0.0)
        changed = True
    if float(priority_score or 0.0) > float(row.base_priority or 0.0):
        row.base_priority = float(priority_score or 0.0)
        changed = True
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/unit/crawler/test_db.py::test_graph_node_tracks_base_priority_raise_only -v`
Expected: PASS

- [ ] **Step 7: Run the existing graph DB tests to confirm no regression**

Run: `uv run pytest tests/unit/crawler/test_db.py -q -k "graph"`
Expected: PASS — the three existing graph tests (`test_crawl_graph_node_and_edge_upsert_dedupes_and_merges_metadata`, `test_crawl_graph_ready_nodes_sort_by_priority_and_status`, `test_ensure_runtime_schema_creates_crawl_graph_tables_for_existing_db`) plus the new one all pass. (Adding a column with a default does not affect existing field/ordering assertions.)

- [ ] **Step 8: Commit**

```bash
git add src/agents/crawler/models.py src/agents/crawler/db/schema.py src/agents/crawler/db/graph.py tests/unit/crawler/test_db.py
git commit -m "feat(graph): store intrinsic base_priority on crawl graph nodes"
```

---

### Task 2: Re-open non-`DONE` terminal nodes on re-discovery (B2)

**Bug (B2):** `_status_rank` (`db/graph.py:419-428`) gives `DONE` and `FAILED` the same rank (`4`), and `upsert_graph_node` (`:132`) only moves status "up" the rank. So a `FAILED` node that is re-discovered as `PENDING` (rank `0 < 4`) keeps `FAILED` forever and is never re-claimed. **Fix:** give `DONE` a strictly higher rank than `FAILED`, and make re-discovery (incoming `PENDING`/`RETRY`) of a non-`DONE` terminal node (`FAILED`/`SKIPPED`) flip it to `RETRY` (re-claimable), while `DONE` stays sticky.

**Files:**
- Modify: `src/agents/crawler/db/graph.py` (`upsert_graph_node` status block; `_status_rank`; new `_resolve_status_on_upsert`)
- Test: `tests/unit/crawler/test_db.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/crawler/test_db.py` (uses already-present `CrawlGraphNodeStatus` import):

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/crawler/test_db.py::test_graph_node_rediscovery_reopens_failed_but_keeps_done_sticky -v`
Expected: FAIL — `reopened.status` is `failed` (asserts `retry`); re-discovery currently can't lift a `FAILED` node.

- [ ] **Step 3: Replace the status-update block in `upsert_graph_node`**

In `src/agents/crawler/db/graph.py`, in `upsert_graph_node` (update path), this block:

```python
    if _status_rank(status_value) >= _status_rank(row.status) and row.status != status_value:
        row.status = status_value
        changed = True
```

becomes:

```python
    resolved_status = _resolve_status_on_upsert(row.status, status_value)
    if resolved_status != row.status:
        row.status = resolved_status
        changed = True
```

- [ ] **Step 4: Add `_resolve_status_on_upsert` and give `DONE` its own rank**

In `src/agents/crawler/db/graph.py`, replace the existing `_status_rank` function:

```python
def _status_rank(status: str) -> int:
    ranks = {
        CrawlGraphNodeStatus.PENDING.value: 0,
        CrawlGraphNodeStatus.RETRY.value: 1,
        CrawlGraphNodeStatus.IN_PROGRESS.value: 2,
        CrawlGraphNodeStatus.SKIPPED.value: 3,
        CrawlGraphNodeStatus.DONE.value: 4,
        CrawlGraphNodeStatus.FAILED.value: 4,
    }
    return ranks.get(str(status or ""), 0)
```

with the version below (note `FAILED=4`, `DONE=5` are now distinct — B2 "terminal states get their own rank") plus the new resolver:

```python
def _status_rank(status: str) -> int:
    ranks = {
        CrawlGraphNodeStatus.PENDING.value: 0,
        CrawlGraphNodeStatus.RETRY.value: 1,
        CrawlGraphNodeStatus.IN_PROGRESS.value: 2,
        CrawlGraphNodeStatus.SKIPPED.value: 3,
        CrawlGraphNodeStatus.FAILED.value: 4,
        CrawlGraphNodeStatus.DONE.value: 5,
    }
    return ranks.get(str(status or ""), 0)


def _resolve_status_on_upsert(current: str, incoming: str) -> str:
    """Decide a graph node's status when it is re-upserted (re-discovered).

    DONE is terminal-sticky. A non-DONE terminal node (FAILED/SKIPPED) that is
    re-discovered as PENDING/RETRY is re-opened to RETRY so it becomes
    claimable again (B2). Otherwise status may only move "up" the rank.
    """
    current = str(current or "")
    incoming = str(incoming or "")
    if not incoming or incoming == current:
        return current
    done = CrawlGraphNodeStatus.DONE.value
    if current == done:
        return done
    reopen_from = {CrawlGraphNodeStatus.FAILED.value, CrawlGraphNodeStatus.SKIPPED.value}
    reopen_with = {CrawlGraphNodeStatus.PENDING.value, CrawlGraphNodeStatus.RETRY.value}
    if current in reopen_from and incoming in reopen_with:
        return CrawlGraphNodeStatus.RETRY.value
    if _status_rank(incoming) >= _status_rank(current):
        return incoming
    return current
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/unit/crawler/test_db.py::test_graph_node_rediscovery_reopens_failed_but_keeps_done_sticky -v`
Expected: PASS

- [ ] **Step 6: Run the graph DB tests to confirm no regression**

Run: `uv run pytest tests/unit/crawler/test_db.py -q -k "graph"`
Expected: PASS. (`_status_rank` is used only by `upsert_graph_node`; the ordering test does not exercise re-discovery of terminal nodes.)

- [ ] **Step 7: Commit**

```bash
git add src/agents/crawler/db/graph.py tests/unit/crawler/test_db.py
git commit -m "fix(graph): re-open non-DONE terminal nodes to RETRY on re-discovery (B2)"
```

---

### Task 3: Recover stale `IN_PROGRESS` graph nodes (B1)

**Bug (B1):** there is no analogue of `recover_stale_in_progress_crawl_tasks` for graph nodes, so a node left `IN_PROGRESS` by a crashed/interrupted run is never reclaimed. **Fix:** add `recover_stale_in_progress_graph_nodes`, a single bulk `UPDATE` that flips every `IN_PROGRESS` node to `RETRY` with `last_error="recovered_stale_in_progress"` — the exact mirror of the crawl-task version (`db/tasks.py:332`). Phase 1b's driver calls it at run start.

**Files:**
- Modify: `src/agents/crawler/db/graph.py` (import `update`; new function; `__all__`)
- Modify: `src/agents/crawler/db/__init__.py` (re-export)
- Test: `tests/unit/crawler/test_db.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/crawler/test_db.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/crawler/test_db.py::test_recover_stale_in_progress_graph_nodes_resets_only_in_progress -v`
Expected: FAIL — `AttributeError: module 'agents.crawler.db' has no attribute 'recover_stale_in_progress_graph_nodes'`.

- [ ] **Step 3: Add the `update` import**

In `src/agents/crawler/db/graph.py`, the SQLAlchemy import line currently reads:

```python
from sqlalchemy import and_, or_, select
```

Change it to include `update`:

```python
from sqlalchemy import and_, or_, select, update
```

- [ ] **Step 4: Add the recovery function**

In `src/agents/crawler/db/graph.py`, add this function immediately after `list_ready_graph_nodes` (before `record_graph_node_result`):

```python
async def recover_stale_in_progress_graph_nodes(session: AsyncSession) -> int:
    """Reset orphaned IN_PROGRESS graph nodes to RETRY (B1, spec §4.4).

    The mirror of recover_stale_in_progress_crawl_tasks: a single driver owns
    IN_PROGRESS, so on run start any IN_PROGRESS node is a crash leftover and
    must become claimable again.
    """
    result = await session.execute(
        update(CrawlGraphNode)
        .where(CrawlGraphNode.status == CrawlGraphNodeStatus.IN_PROGRESS.value)
        .values(
            status=CrawlGraphNodeStatus.RETRY.value,
            last_error="recovered_stale_in_progress",
            updated_at=_now_utc(),
        )
    )
    await session.flush()
    return int(result.rowcount or 0)
```

- [ ] **Step 5: Export it from `graph.py` `__all__`**

In `src/agents/crawler/db/graph.py`, the `__all__` list ends with:

```python
__all__ = [
    "get_graph_node_by_key",
    "graph_node_key",
    "list_ready_graph_nodes",
    "mark_graph_node_status",
    "record_graph_node_result",
    "upsert_graph_edge",
    "upsert_graph_node",
]
```

Add `recover_stale_in_progress_graph_nodes` (keep alphabetical-ish ordering):

```python
__all__ = [
    "get_graph_node_by_key",
    "graph_node_key",
    "list_ready_graph_nodes",
    "mark_graph_node_status",
    "recover_stale_in_progress_graph_nodes",
    "record_graph_node_result",
    "upsert_graph_edge",
    "upsert_graph_node",
]
```

- [ ] **Step 6: Re-export from the `crawler_db` facade**

In `src/agents/crawler/db/__init__.py`, the graph import block reads:

```python
from agents.crawler.db.graph import (
    get_graph_node_by_key,
    graph_node_key,
    list_ready_graph_nodes,
    mark_graph_node_status,
    record_graph_node_result,
    upsert_graph_edge,
    upsert_graph_node,
)
```

Add the new name:

```python
from agents.crawler.db.graph import (
    get_graph_node_by_key,
    graph_node_key,
    list_ready_graph_nodes,
    mark_graph_node_status,
    recover_stale_in_progress_graph_nodes,
    record_graph_node_result,
    upsert_graph_edge,
    upsert_graph_node,
)
```

And in the module-level `__all__` list, add the string `"recover_stale_in_progress_graph_nodes",` next to the existing `"recover_stale_in_progress_crawl_tasks",` entry.

- [ ] **Step 7: Run the test to verify it passes**

Run: `uv run pytest tests/unit/crawler/test_db.py::test_recover_stale_in_progress_graph_nodes_resets_only_in_progress -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/agents/crawler/db/graph.py src/agents/crawler/db/__init__.py tests/unit/crawler/test_db.py
git commit -m "feat(graph): recover stale in_progress graph nodes to RETRY (B1)"
```

---

### Task 4: Atomic claim by effective priority (`claim_next_graph_node`) (D3 + B3)

The driver (Phase 1b) needs to **claim** one node at a time: select the best ready (`PENDING`/`RETRY`) node and flip it to `IN_PROGRESS` in a single transaction. Ordering uses the **effective priority** `base_priority − attempt_count × penalty`, so attempt-backoff is honored at claim time and is **not** erased when re-discovery raises `base_priority`/`priority_score` (B3). With a single driver, one select-then-flip transaction is atomic by construction (spec §4.2).

**Files:**
- Modify: `src/agents/crawler/db/graph.py` (penalty constant; new function; `__all__`)
- Modify: `src/agents/crawler/db/__init__.py` (re-export)
- Test: `tests/unit/crawler/test_db.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/crawler/test_db.py`:

```python
async def test_claim_next_graph_node_honors_backoff_across_rediscovery(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "graph_claim_backoff.db"))
    await db.init_db()
    async with db.session() as session:
        fresh = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/fresh",
            org_unit_name="CS",
            priority_score=80,
        )
        backed_off = await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/backed-off",
            org_unit_name="CS",
            priority_score=80,
        )
        # Two retry attempts back this node off (attempt_count -> 2).
        await crawler_db.mark_graph_node_status(
            session, backed_off.id, status=CrawlGraphNodeStatus.RETRY, increment_attempt=True
        )
        await crawler_db.mark_graph_node_status(
            session, backed_off.id, status=CrawlGraphNodeStatus.RETRY, increment_attempt=True
        )
        # Re-discovery raises its intrinsic priority again — must NOT erase backoff (B3).
        await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/backed-off",
            org_unit_name="CS",
            priority_score=80,
        )

        first = await crawler_db.claim_next_graph_node(session)
        assert first.id == fresh.id
        assert first.status == CrawlGraphNodeStatus.IN_PROGRESS.value

        second = await crawler_db.claim_next_graph_node(session)
        assert second.id == backed_off.id
        assert second.status == CrawlGraphNodeStatus.IN_PROGRESS.value

        # Both nodes are now IN_PROGRESS, so nothing is claimable.
        assert await crawler_db.claim_next_graph_node(session) is None
    await db.close()


async def test_claim_next_graph_node_filters_by_node_type(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "graph_claim_filter.db"))
    await db.init_db()
    async with db.session() as session:
        await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/faculty",
            org_unit_name="CS",
            priority_score=80,
        )
        # No DETAIL_URL nodes exist, so a DETAIL-scoped claim returns None.
        assert (
            await crawler_db.claim_next_graph_node(
                session, node_types=[CrawlGraphNodeType.DETAIL_URL]
            )
            is None
        )
    await db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/crawler/test_db.py -v -k claim_next_graph_node`
Expected: FAIL — `AttributeError: module 'agents.crawler.db' has no attribute 'claim_next_graph_node'`.

- [ ] **Step 3: Add the backoff penalty constant**

In `src/agents/crawler/db/graph.py`, just below the `_GRAPH_NODE_METADATA_KEYS = {...}` block near the top of the module, add:

```python
# Per-attempt penalty subtracted from base_priority to compute claim-time
# effective priority. Matches the legacy -5.0 attempt step in
# mark_graph_node_status so claim ordering and backoff stay consistent.
_ATTEMPT_BACKOFF_PENALTY = 5.0
```

- [ ] **Step 4: Add `claim_next_graph_node`**

In `src/agents/crawler/db/graph.py`, add this function immediately after `list_ready_graph_nodes`:

```python
async def claim_next_graph_node(
    session: AsyncSession,
    *,
    node_types: Iterable[str | CrawlGraphNodeType] | None = None,
    org_unit_names: Iterable[str] | None = None,
    org_unit_ids: Iterable[int] | None = None,
) -> CrawlGraphNode | None:
    """Atomically claim the highest-priority ready node: select the best
    PENDING/RETRY node and flip it to IN_PROGRESS in one transaction.

    Ordering uses effective priority (base_priority - attempt_count * penalty)
    so attempt-backoff is honored and survives re-discovery (B3). A single
    driver makes the select-then-flip atomic by construction (spec §4.2).
    """
    filters: list[Any] = [
        CrawlGraphNode.status.in_(
            [CrawlGraphNodeStatus.PENDING.value, CrawlGraphNodeStatus.RETRY.value]
        )
    ]
    if node_types:
        type_values = [_enum_value(item) for item in node_types]
        filters.append(CrawlGraphNode.type.in_(type_values))
    normalized_names = [
        normalize_org_unit_name(name, default="")
        for name in (org_unit_names or [])
        if normalize_org_unit_name(name, default="")
    ]
    normalized_ids = [int(org_unit_id) for org_unit_id in (org_unit_ids or []) if org_unit_id is not None]
    org_filters: list[Any] = []
    if normalized_names:
        org_filters.append(CrawlGraphNode.org_unit_name.in_(normalized_names))
    if normalized_ids:
        org_filters.append(CrawlGraphNode.org_unit_id.in_(normalized_ids))
    if org_filters:
        filters.append(or_(*org_filters))

    effective_priority = CrawlGraphNode.base_priority - (
        CrawlGraphNode.attempt_count * _ATTEMPT_BACKOFF_PENALTY
    )
    row = (
        await session.execute(
            select(CrawlGraphNode)
            .where(and_(*filters))
            .order_by(
                effective_priority.desc(),
                CrawlGraphNode.confidence.desc(),
                CrawlGraphNode.depth.asc(),
                CrawlGraphNode.attempt_count.asc(),
                CrawlGraphNode.id.asc(),
            )
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return None
    row.status = CrawlGraphNodeStatus.IN_PROGRESS.value
    row.updated_at = _now_utc()
    await session.flush()
    return row
```

- [ ] **Step 5: Export `claim_next_graph_node`**

In `src/agents/crawler/db/graph.py` `__all__`, add `"claim_next_graph_node",` as the first entry. In `src/agents/crawler/db/__init__.py`, add `claim_next_graph_node` to the `from agents.crawler.db.graph import (...)` block and add `"claim_next_graph_node",` to the module `__all__`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/crawler/test_db.py -v -k claim_next_graph_node`
Expected: PASS (both tests).

- [ ] **Step 7: Commit**

```bash
git add src/agents/crawler/db/graph.py src/agents/crawler/db/__init__.py tests/unit/crawler/test_db.py
git commit -m "feat(graph): atomic claim_next_graph_node ordered by effective priority (B3)"
```

---

### Task 5: Foundations gate — full crawler suite green

This phase is additive (no crawler control-flow uses the new functions yet), so the entire crawler test suite — including the green, uncommitted traversal-only work — must still pass.

**Files:** none (verification only).

- [ ] **Step 1: Run the full DB suite**

Run: `uv run pytest tests/unit/crawler/test_db.py -q`
Expected: PASS (all existing tests + the 5 new ones from Tasks 1–4).

- [ ] **Step 2: Run the full crawler + runtime suites**

Run: `uv run pytest tests/unit/crawler tests/unit/runtime -q`
Expected: PASS. If anything fails, STOP and report — a foundations change must not alter existing behavior. Do not paper over a failure by weakening an assertion.

- [ ] **Step 3: Confirm the traversal-only work is still uncommitted and intact**

Run: `git status --short`
Expected: the Phase 1a commits are made; the pre-existing traversal-only edits (`extraction_pipeline.py`, `extraction_payloads.py`, `agent.py`, `agent_detail.py`, `prompt_builder.py`, `session_state.py`, `skills/save-professors.md`, `tests/unit/crawler/test_agent.py`, `tests/unit/crawler/test_extraction_modules.py`) remain modified/uncommitted. They are carried into Phase 1b — do not commit or revert them here.

---

## Self-Review

- **Spec coverage:**
  - §4.1 node lifecycle / effective priority — `base_priority` column (Task 1) + effective-priority ordering at claim (Task 4). ✅
  - §4.2 claim/consume (atomic single-claimer claim) — `claim_next_graph_node` flips PENDING/RETRY → IN_PROGRESS in one transaction (Task 4). ✅
  - §4.4 recovery — `recover_stale_in_progress_graph_nodes` (Task 3). ✅ (Frontier re-derive from `org_units`/`page_cache`/retryable failures is driver-side, deferred to Phase 1b.)
  - §5 B1 — Task 3. ✅  §5 B2 — Task 2. ✅  §5 B3 — Tasks 1 + 4 (store `base_priority`, subtract backoff only at claim). ✅
  - §5 B4/B5/B6 and §4.2 worker terminal-status ownership, §4.3 WAF single-fetch invariant test, §6 handler/concurrency/recovery integration tests — **out of scope for 1a; these are Phase 1b** (they require the driver). Noted in the header, not a gap.
- **Placeholders:** none — every code step shows the exact before/after and every command shows the exact `uv run pytest` invocation with expected output.
- **Type/name consistency:** `base_priority` (column), `_resolve_status_on_upsert`, `_status_rank`, `recover_stale_in_progress_graph_nodes`, `claim_next_graph_node`, `_ATTEMPT_BACKOFF_PENALTY` are referenced identically across the model, `db/graph.py`, `db/schema.py`, `db/__init__.py`, and tests. `claim_next_graph_node`'s filter signature mirrors `list_ready_graph_nodes` exactly (`node_types`/`org_unit_names`/`org_unit_ids`).
- **Ships green:** Tasks 1–4 each end green; Task 5 gates the whole crawler + runtime suite. The legacy `mark_graph_node_status` `priority_score −= 5.0` step is intentionally left untouched (the existing `test_crawl_graph_ready_nodes_sort_by_priority_and_status` assertion `priority_score == 35` stays valid); claim reads `base_priority`, not `priority_score`, so backoff is correct regardless. That vestigial step is removed in Phase 3 cleanup.
- **No behavior change:** the crawler driver still uses the in-memory BFS and `list_ready_graph_nodes`; nothing calls `claim_next_graph_node` / `recover_stale_in_progress_graph_nodes` until Phase 1b, so production behavior is unchanged.
