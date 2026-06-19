# Graph Crawler — Phase 1b: Claim-Driver Rewrite (faculty-onward graph-as-queue) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `crawl_graph_nodes` the live work queue for the faculty-onward pipeline. Replace the in-memory BFS in `_extract_professors` with a **single fetch driver** that atomically claims nodes (`claim_next_graph_node`, from Phase 1a), fetches them serially (WAF invariant), and dispatches by node type — list/pagination/followup pages traverse-and-discover (mark `DONE`), detail pages enqueue an LLM job and are marked terminal **by the worker**. Wire stale-node recovery (B1), and land B4/B5/B6 + a transient-vs-permanent failure classification.

**Architecture:** Today `_extract_professors` (`extraction_pipeline.py:115`) seeds from the graph once, then runs an in-memory `pages_to_process` BFS over `_QueuedUrl`; discovery upserts PENDING graph nodes **and** appends to that in-memory list (a duplicate frontier), and detail pages are fetched inline by `agent_detail.process_detail_urls_with_human`. Phase 1b deletes the in-memory frontier: the driver loop is `claim_next_graph_node([faculty_list, pagination, followup, detail]) → fetch → dispatch`. Discovery keeps upserting PENDING children (they become claimable); detail discovery becomes **upsert-only** (the driver claims and fetches detail nodes, then hands `(node, FetchResult)` to the existing LLM/DB worker pool, which already owns terminal node status). Per-college locality is preserved by **priority inheritance** (a faculty page's detail children inherit the parent's priority + a boost) and by org-scoped claiming in streaming mode. Termination: claim until empty → `await llm_queue.join()/db_queue.join()` → one final claim re-check (workers create no navigation nodes) → stop.

**Tech Stack:** Python, asyncio, SQLAlchemy 2.0 async, aiosqlite, pytest + pytest-asyncio (`asyncio_mode=auto`), `uv` runner.

**Source spec:** `docs/superpowers/specs/2026-06-10-graph-crawler-migration-design.md` — §4.2 (claim/consume loop & worker pool, Approach A), §4.3 (WAF single-fetch invariant), §4.4 (recovery), §5 (B4, B5, B6 + transient/permanent classification), §6 (testing). Builds directly on **Phase 1a** (`claim_next_graph_node`, `recover_stale_in_progress_graph_nodes`, `base_priority`, B1/B2/B3) — already committed on `neo`.

**Scope boundaries (kept, per spec phasing):**
- **Keep** the `_is_interactive` streaming fork and `_find_and_extract_streaming_impl` (removed in **Phase 2**). Streaming still calls `_extract_professors(faculty_for_unit)` per org unit; the driver is org-scoped so per-college locality holds.
- **Keep** `crawl_tasks` writes and the dual node+task status writes inside the worker pool / `_record_list_page_traversal_task` / `_enqueue_extraction_task` (the write path is retired in **Phase 3**). Phase 1b changes *who drives*, not the persistence side-channel.
- **Keep** `crawl_graph_edges` writes via `record_discovered_links` (stopped in Phase 3).
- **Remove** the crawl_tasks-based recovery (`_recover_pipeline_tasks` and friends) from the driver — graph-node recovery (B1) + claim re-derive replaces it. The resume tests that asserted crawl_tasks recovery are rewritten to assert graph-node recovery.

**Baseline:** branch `neo`, on top of Phase 1a (HEAD `80f8dcf`) and the traversal-only commit `25977b3`. Run the full suite green at the start: `uv run pytest tests/unit/crawler tests/unit/runtime -q` → expect `334 passed, 6 skipped`.

---

## File-structure overview (what each task touches)

- `src/agents/crawler/graph_frontier.py` — add `claim_next()` and `recover_stale_in_progress()` wrappers (Task 1).
- `src/agents/crawler/extraction_pipeline.py` — replace `_extract_professors`; add `_run_claim_driver`, `_drive_graph_node`, `_drive_list_node`, `_drive_detail_node`, `_seed_faculty_link_nodes`, `_discover_related_page_nodes`; delete the in-memory BFS, `_seed_frontier_items`, `_recover_pipeline_tasks` and the crawl_tasks recovery helpers (Tasks 2, 4, 5, 6).
- `src/agents/crawler/agent_detail.py` — `enrich_profiles_with_human` becomes detail-node **upsert-only** with priority inheritance; the inline-fetch `process_detail_urls_with_human` is removed (Task 2).
- `tests/unit/crawler/test_agent.py` — rewrite the BFS/`fetcher.calls`-ordering and crawl_tasks-recovery slice; add handler/recovery tests (Tasks 2, 7).
- `tests/unit/crawler/test_extraction_modules.py` — add the WAF single-fetch invariant test and the driver/concurrency tests (Tasks 3, 6, 7).

---

### Task 1: `GraphFrontier.claim_next` and `recover_stale_in_progress` wrappers

Thin async wrappers so the driver works through `self.graph_frontier` (consistent with every other graph call) and returns a `GraphFetchCandidate` rather than a raw ORM row.

**Files:**
- Modify: `src/agents/crawler/graph_frontier.py` (two methods on `GraphFrontier`)
- Test: `tests/unit/crawler/test_extraction_modules.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/crawler/test_extraction_modules.py`. Check the top of that file for an existing minimal-agent/frontier fixture; if none exposes a bare `GraphFrontier`, construct one against a real DB the same way `test_db.py` does. Use this self-contained test:

```python
from types import SimpleNamespace

from agents.crawler import db as crawler_db
from agents.crawler.graph_frontier import GraphFrontier
from agents.crawler.models import CrawlGraphNodeStatus, CrawlGraphNodeType
from runtime.database import DatabaseManager
from tests.conftest import sqlite_url


async def test_graph_frontier_claim_next_and_recover_stale(tmp_path):
    db = DatabaseManager(sqlite_url(tmp_path / "frontier_claim.db"))
    await db.init_db()
    frontier = GraphFrontier(SimpleNamespace(db=db, start_url="https://www.example.edu.cn/"))

    async with db.session() as session:
        await crawler_db.upsert_graph_node(
            session,
            node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            url="https://cs.example.edu.cn/faculty",
            org_unit_name="CS",
            priority_score=85,
            status=CrawlGraphNodeStatus.PENDING,
        )

    claimed = await frontier.claim_next(node_types=[CrawlGraphNodeType.FACULTY_LIST_URL])
    assert claimed is not None
    assert claimed.url == "https://cs.example.edu.cn/faculty"
    assert claimed.status == CrawlGraphNodeStatus.IN_PROGRESS.value
    # Already IN_PROGRESS → nothing else claimable.
    assert await frontier.claim_next(node_types=[CrawlGraphNodeType.FACULTY_LIST_URL]) is None

    # A stale IN_PROGRESS node is reset to RETRY and becomes claimable again.
    recovered = await frontier.recover_stale_in_progress()
    assert recovered == 1
    reclaimed = await frontier.claim_next(node_types=[CrawlGraphNodeType.FACULTY_LIST_URL])
    assert reclaimed is not None
    assert reclaimed.url == "https://cs.example.edu.cn/faculty"
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/crawler/test_extraction_modules.py::test_graph_frontier_claim_next_and_recover_stale -v`
Expected: FAIL — `AttributeError: 'GraphFrontier' object has no attribute 'claim_next'`.

- [ ] **Step 3: Add the wrappers**

In `src/agents/crawler/graph_frontier.py`, add these two methods to `GraphFrontier` immediately after `next_fetch_candidates` (so claim/recover sit beside the read-only frontier query):

```python
    async def claim_next(
        self,
        *,
        node_types: Iterable[str | CrawlGraphNodeType] | None = None,
        org_unit_names: Iterable[str] | None = None,
        org_unit_ids: Iterable[int] | None = None,
    ) -> GraphFetchCandidate | None:
        async with self.agent.db.session() as session:
            row = await crawler_db.claim_next_graph_node(
                session,
                node_types=node_types,
                org_unit_names=org_unit_names,
                org_unit_ids=org_unit_ids,
            )
            if row is None:
                return None
            return self._candidate_from_node(row)

    async def recover_stale_in_progress(self) -> int:
        async with self.agent.db.session() as session:
            return await crawler_db.recover_stale_in_progress_graph_nodes(session)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/crawler/test_extraction_modules.py::test_graph_frontier_claim_next_and_recover_stale -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/agents/crawler/graph_frontier.py tests/unit/crawler/test_extraction_modules.py
git commit -m "feat(crawler): GraphFrontier.claim_next + recover_stale_in_progress wrappers"
```

---

### Task 2: Replace the in-memory BFS with the claim-driver (the core)

This is the central change. `_extract_professors` becomes: recover stale nodes → seed faculty links as PENDING nodes → start the worker pool → run the claim-driver → drain & shut down. Discovery still upserts PENDING children (now the *only* frontier); detail discovery becomes upsert-only and the driver claims detail nodes. Because the per-node skip/traverse/enqueue helpers already exist (`_record_list_page_traversal_task`, `_enqueue_extraction_task`, `_mark_retryable_fetch_failure`, the discovery in `_schedule_related_pages`, `enrich_profiles_with_human`), the driver mostly re-wires existing pieces.

**Files:**
- Modify: `src/agents/crawler/extraction_pipeline.py` (rewrite `_extract_professors`; add driver methods; delete BFS helpers `_seed_frontier_items`, the `_schedule_related_pages` in-memory append, `_recover_pipeline_tasks`, `_mark_recovered_list_task_suppressed`, `_should_skip_recovered_task`, `_recovered_task_needs_refetch`, `_refetch_recovered_detail_task`, `_mark_recovered_refetch_retry`, `_mark_recovered_task_terminal`)
- Modify: `src/agents/crawler/agent_detail.py` (`enrich_profiles_with_human` → upsert-only + priority inheritance; remove `process_detail_urls_with_human` inline fetch)
- Test: `tests/unit/crawler/test_agent.py` (rewrite the affected slice — see Step 6)

> **Note on size:** this task is large but its sub-steps each end runnable. Implement in the sub-step order below; run the targeted tests named in each sub-step before moving on.

- [ ] **Step 1: Write the failing handler test (drives the new shape)**

Add to `tests/unit/crawler/test_agent.py` a test that seeds one faculty-list node whose page links to one detail page, runs `_extract_professors`, and asserts the driver fetched both (serially) and the detail page produced a saved professor. Mirror the existing `_agent(...)` fixture used by `test_agent_manual_faculty_entrance_bypasses_discovery` (which already wires a fake fetcher keyed by URL + a `FakeLLM` that saves a professor for `EXTRACT_PROFESSORS`). Concretely:

```python
async def test_extract_professors_claims_faculty_then_detail_from_graph(tmp_path):
    agent, db = await _agent(
        tmp_path,
        fetch_map={
            "https://www.example.edu.cn/cs/faculty": FetchResult(
                "https://www.example.edu.cn/cs/faculty",
                "faculty roster",
                ["https://www.example.edu.cn/cs/info/1001/ada.htm"],
                200,
            ),
            "https://www.example.edu.cn/cs/info/1001/ada.htm": FetchResult(
                "https://www.example.edu.cn/cs/info/1001/ada.htm",
                "faculty detail Ada Professor email ada@example.edu.cn",
                [],
                200,
            ),
        },
    )
    # Seed one faculty-list node, then run the claim-driver.
    await agent.graph_frontier.ensure_url_node(
        url="https://www.example.edu.cn/cs/faculty",
        node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
        org_unit_name="计算机学院",
        status=CrawlGraphNodeStatus.PENDING,
    )
    await agent._extract_professors(
        [_QueuedUrl("https://www.example.edu.cn/cs/faculty", 1, label="计算机学院")]
    )

    assert agent.fetcher.calls == [
        "https://www.example.edu.cn/cs/faculty",
        "https://www.example.edu.cn/cs/info/1001/ada.htm",
    ]
    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        nodes = (await session.execute(select(CrawlGraphNode))).scalars().all()
    assert [p.name for p in professors] == ["Ada Professor"]
    statuses = {n.url: n.status for n in nodes}
    assert statuses["https://www.example.edu.cn/cs/faculty"] == CrawlGraphNodeStatus.DONE.value
    assert statuses["https://www.example.edu.cn/cs/info/1001/ada.htm"] == CrawlGraphNodeStatus.DONE.value
    await db.close()
```

> If the exact `_agent(...)` signature differs (e.g. it takes a `fetcher=` map under another name), adapt the call to the real helper at the top of `test_agent.py` — do not invent a new fixture. The behavioral assertions (serial fetch order, one professor, both nodes `DONE`) are the contract.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/crawler/test_agent.py::test_extract_professors_claims_faculty_then_detail_from_graph -v`
Expected: FAIL — with the current BFS, the detail node is fetched inline (not claimed); the test will fail on the node-status assertions and/or fetch order once the driver is in place. (Before the rewrite it may error or mis-order — that's the red.)

- [ ] **Step 3: Add the driver constants and `_seed_faculty_link_nodes`**

In `src/agents/crawler/extraction_pipeline.py`, near the top-of-class constants, add:

```python
# Node types the faculty-onward driver claims (Phase 1b; org-listing/org-unit
# discovery is still done outside the driver until Phase 2).
_DRIVER_NODE_TYPES = (
    CrawlGraphNodeType.FACULTY_LIST_URL,
    CrawlGraphNodeType.PAGINATION_URL,
    CrawlGraphNodeType.FACULTY_FOLLOWUP_URL,
    CrawlGraphNodeType.DETAIL_URL,
)
# Detail children inherit the parent faculty page's priority plus this boost so a
# college's subtree is claimed contiguously (per-college locality, spec §4.2).
_DETAIL_PRIORITY_INHERIT_BOOST = 20.0
```

Add `_seed_faculty_link_nodes` (idempotent PENDING upsert; `_resolve_status_on_upsert` keeps `DONE` sticky and re-opens `FAILED`→`RETRY` from Phase 1a):

```python
    async def _seed_faculty_link_nodes(self, faculty_links: list[_QueuedUrl]) -> None:
        for item in faculty_links:
            node_type = item.graph_node_type or CrawlGraphNodeType.FACULTY_LIST_URL.value
            await self.graph_frontier.ensure_url_node(
                url=item.identity_url or item.url,
                node_type=node_type,
                org_unit_name=item.label,
                org_unit_id=item.org_unit_id,
                depth=item.depth,
                metadata={
                    "fetch_url": item.url,
                    "identity_url": item.identity_url,
                    "fetch_action": item.fetch_action,
                    "source": "extraction_seed",
                },
            )
```

- [ ] **Step 4: Replace `_extract_professors` and add the driver methods**

In `src/agents/crawler/extraction_pipeline.py`, replace the entire current `_extract_professors` (the seed + `if not self.pipeline_enabled:` BFS + pipeline BFS + `finally` block, roughly lines 115–564) with the driver version below, and add the four `_run_claim_driver`/`_drive_*`/`_discover_related_page_nodes` methods after it:

```python
    async def _extract_professors(
        self,
        faculty_links: list[_QueuedUrl],
        *,
        recovery_limit: int | None = None,
    ) -> None:
        self._log_state(CrawlerState.EXTRACT_PROFESSORS)
        skills = await self._select_skills(CrawlerState.EXTRACT_PROFESSORS)
        self.logger.info(
            "Claim-driver pipeline enabled=%s llm_workers=%s db_workers=%s queue_cap=%s",
            self.pipeline_enabled,
            self.pipeline_llm_workers,
            self.pipeline_db_workers,
            self.pipeline_queue_cap,
        )

        # B1: orphaned IN_PROGRESS nodes from a crashed run become claimable again.
        stale = await self.graph_frontier.recover_stale_in_progress()
        if stale:
            self._pipeline_stats["stale_in_progress_recovered"] = int(
                self._pipeline_stats.get("stale_in_progress_recovered", 0)
            ) + int(stale)
            self.logger.info("Recovered %s stale in_progress graph nodes", stale)

        await self._seed_faculty_link_nodes(faculty_links)

        # Org scope keeps streaming mode per-college; empty (resume) → claim globally.
        org_names = [item.label for item in faculty_links if item.label] or None
        org_ids = [item.org_unit_id for item in faculty_links if item.org_unit_id is not None] or None

        if not self.pipeline_enabled:
            await self._run_claim_driver(
                skills, llm_queue=None, db_queue=None, org_names=org_names, org_ids=org_ids
            )
            return

        llm_queue: asyncio.Queue[_ExtractionTaskItem | None] = asyncio.Queue(maxsize=self.pipeline_queue_cap)
        db_queue: asyncio.Queue[_SaveEvent | None] = asyncio.Queue(maxsize=self.pipeline_queue_cap)
        llm_workers = [
            asyncio.create_task(self._pipeline_llm_worker(llm_queue, db_queue, skills), name=f"llm_worker_{i}")
            for i in range(self.pipeline_llm_workers)
        ]
        db_workers = [
            asyncio.create_task(self._pipeline_db_worker(db_queue), name=f"db_worker_{i}")
            for i in range(self.pipeline_db_workers)
        ]
        try:
            await self._run_claim_driver(
                skills, llm_queue=llm_queue, db_queue=db_queue, org_names=org_names, org_ids=org_ids
            )
        finally:
            await llm_queue.join()
            for _ in llm_workers:
                await llm_queue.put(None)
            await asyncio.gather(*llm_workers, return_exceptions=False)
            await db_queue.join()
            for _ in db_workers:
                await db_queue.put(None)
            await asyncio.gather(*db_workers, return_exceptions=False)
            self._log_pipeline_stats()

    async def _run_claim_driver(
        self,
        skills: str,
        *,
        llm_queue: "asyncio.Queue[_ExtractionTaskItem | None] | None",
        db_queue: "asyncio.Queue[_SaveEvent | None] | None",
        org_names: list[str] | None,
        org_ids: list[int] | None,
    ) -> None:
        while True:
            candidate = await self.graph_frontier.claim_next(
                node_types=_DRIVER_NODE_TYPES,
                org_unit_names=org_names,
                org_unit_ids=org_ids,
            )
            if candidate is None:
                if llm_queue is None:
                    break
                # Let in-flight detail jobs settle; a worker may have set a node to
                # RETRY, which is then claimable on the re-check below.
                await llm_queue.join()
                await db_queue.join()
                candidate = await self.graph_frontier.claim_next(
                    node_types=_DRIVER_NODE_TYPES,
                    org_unit_names=org_names,
                    org_unit_ids=org_ids,
                )
                if candidate is None:
                    break
            await self._drive_graph_node(candidate, skills, llm_queue=llm_queue, db_queue=db_queue)

    async def _drive_graph_node(
        self,
        candidate: "GraphFetchCandidate",
        skills: str,
        *,
        llm_queue: "asyncio.Queue[_ExtractionTaskItem | None] | None",
        db_queue: "asyncio.Queue[_SaveEvent | None] | None",
    ) -> None:
        current = self.graph_frontier.to_queued_url(candidate, _QueuedUrl)
        is_detail = candidate.node_type == CrawlGraphNodeType.DETAIL_URL.value

        if self._is_noise_or_login_candidate(current.url):
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.SKIPPED,
                last_error="noise_or_login_candidate",
            )
            return
        fetched = await self._fetch_url(
            current.url, current.depth, action=current.fetch_action, identity_url=current.identity_url
        )
        if fetched is None:
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.RETRY,
                last_error="fetch_failed",
                increment_attempt=True,
            )
            return
        if is_retryable_fetch_failure(fetched.block_reason):
            await self._mark_retryable_fetch_failure(current, fetched, detail_mode=is_detail)
            return
        if self._is_noise_or_login_candidate(fetched.url):
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.SKIPPED,
                last_error="noise_or_login_page",
            )
            return
        if self._is_retired_page(fetched):
            await self.graph_frontier.mark_node_status(
                current.graph_node_id,
                status=CrawlGraphNodeStatus.SKIPPED,
                last_error="retired_page",
            )
            return

        if is_detail:
            await self._drive_detail_node(current, fetched, skills, llm_queue=llm_queue)
        else:
            await self._drive_list_node(current, fetched, skills, llm_queue=llm_queue)

    async def _drive_list_node(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        skills: str,
        *,
        llm_queue: "asyncio.Queue[_ExtractionTaskItem | None] | None",
    ) -> None:
        # Traversal-only: record the list page DONE (no save) and discover children
        # as PENDING nodes the driver will claim later.
        if await self._record_list_page_traversal_task(current, fetched) == "skipped":
            return
        reserved_urls = await self._discover_related_page_nodes(current, fetched)
        previous_detail_queue = self._active_detail_llm_queue
        self._active_detail_llm_queue = llm_queue
        try:
            await self._enrich_profiles_with_detail_backend(
                current, fetched, skills, reserved_urls=reserved_urls
            )
        finally:
            self._active_detail_llm_queue = previous_detail_queue

    async def _drive_detail_node(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
        skills: str,
        *,
        llm_queue: "asyncio.Queue[_ExtractionTaskItem | None] | None",
    ) -> None:
        if llm_queue is None:
            # Synchronous (pipeline-disabled) path: extract inline and mark terminal.
            saved = await self._extract_professors_from_page(
                current, fetched, skills, detail_mode=True, requested_url=current.queue_url
            )
            if saved >= 0:
                await self.graph_frontier.mark_node_status(
                    current.graph_node_id, status=CrawlGraphNodeStatus.DONE
                )
            return
        # Pipeline path: enqueue the LLM job; the worker pool owns terminal status.
        await self._enqueue_extraction_task(
            current,
            fetched,
            llm_queue=llm_queue,
            detail_mode=True,
            priority=1,
            requested_url=current.queue_url,
        )
```

- [ ] **Step 5: Add `_discover_related_page_nodes` and `_log_pipeline_stats`; delete dead BFS/recovery helpers**

`_discover_related_page_nodes` is the discovery half of the old `_schedule_related_pages` — it extracts followups/pagination and upserts them as PENDING nodes via `record_discovered_links`, **without** the in-memory `pages_to_process` append, and returns the reserved-URL set (so detail enrichment skips links already queued for traversal). Add it next to the driver methods:

```python
    async def _discover_related_page_nodes(
        self,
        current: _QueuedUrl,
        fetched: FetchResult,
    ) -> set[str]:
        def _key(url: str) -> str:
            return _sanitize_url(url) or (url or "").strip()

        followup_items: list[_QueuedUrl] = []
        for link in self._extract_followup_faculty_links(fetched.links, fetched.url)[:_FOLLOWUP_PAGE_LIMIT]:
            next_depth = current.depth + 1
            if not self._within_depth(next_depth):
                continue
            followup_items.append(
                _QueuedUrl(url=link, depth=next_depth, label=current.label, org_unit_id=current.org_unit_id)
            )

        pagination_items: list[_QueuedUrl] = []
        for plink in self._extract_pagination_links(fetched.links, fetched.url):
            if not self._within_depth(current.depth):
                continue
            pagination_items.append(
                _QueuedUrl(url=plink, depth=current.depth, label=current.label, org_unit_id=current.org_unit_id)
            )
        for state in getattr(fetched, "pagination_states", ()) or ():
            action = form_pagination.pagination_state_to_fetch_action(state)
            if not action:
                continue
            identity_url = str(action.get("synthetic_url") or "").strip()
            if not identity_url:
                continue
            pagination_items.append(
                _QueuedUrl(
                    url=str(action.get("url") or fetched.url),
                    depth=current.depth,
                    label=current.label,
                    org_unit_id=current.org_unit_id,
                    fetch_action=action,
                    identity_url=identity_url,
                )
            )

        if followup_items:
            await self.graph_frontier.record_discovered_links(
                source_url=fetched.url,
                links=followup_items,
                node_type=CrawlGraphNodeType.FACULTY_FOLLOWUP_URL,
                edge_type=CrawlGraphEdgeType.DISCOVERED_ON_PAGE,
                source_node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
                org_unit_name=current.label,
                org_unit_id=current.org_unit_id,
                depth=current.depth + 1,
                confidence=0.8,
                metadata={"source": "faculty_followup"},
            )
            self._pipeline_stats["followups_scheduled"] = int(
                self._pipeline_stats.get("followups_scheduled", 0)
            ) + len(followup_items)
        if pagination_items:
            await self.graph_frontier.record_discovered_links(
                source_url=fetched.url,
                links=pagination_items,
                node_type=CrawlGraphNodeType.PAGINATION_URL,
                edge_type=CrawlGraphEdgeType.PAGINATION_OF,
                source_node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
                org_unit_name=current.label,
                org_unit_id=current.org_unit_id,
                depth=current.depth,
                confidence=0.9,
                metadata={"source": "pagination"},
            )
            self._pipeline_stats["pagination_scheduled"] = int(
                self._pipeline_stats.get("pagination_scheduled", 0)
            ) + len(pagination_items)

        return {
            _key(item.queue_url)
            for item in (followup_items + pagination_items)
            if _key(item.queue_url)
        }
```

Move the big "Extraction pipeline stats …" `self.logger.info(...)` block from the old `finally` into a `_log_pipeline_stats(self) -> None` method and call it from the new `finally` (above). Then **delete** the now-unreachable helpers: `_seed_frontier_items` (was nested), `_recover_pipeline_tasks`, `_mark_recovered_list_task_suppressed`, `_should_skip_recovered_task`, `_recovered_task_needs_refetch`, `_refetch_recovered_detail_task`, `_mark_recovered_refetch_retry`, `_mark_recovered_task_terminal`. (Grep for each name across the package first; only delete once there are no remaining references outside the deleted block.)

- [ ] **Step 6: Make detail discovery upsert-only in `agent_detail.py` (with priority inheritance)**

In `src/agents/crawler/agent_detail.py`, `enrich_profiles_with_human` currently ends by calling `self._process_detail_urls_with_human(pending, current, skills)` (the inline fetch). Replace the priority used when building `pending` candidates so detail children inherit the parent faculty page's priority, and **remove** the inline-fetch call so the PENDING detail nodes are left for the driver to claim. Concretely:

In the `pending.append(GraphFetchCandidate(...))` block, change the `priority_score=` argument from
```python
                priority_score=self.graph_frontier.priority_for(
                    CrawlGraphNodeType.DETAIL_URL,
                    url=normalized,
                    depth=current.depth + 1,
                )
                - index * 0.01,
```
to inherit the parent's priority plus the boost (falls back to the type base when the parent priority is unknown):
```python
                priority_score=max(
                    float(getattr(current, "graph_priority_score", 0.0) or 0.0)
                    + _DETAIL_PRIORITY_INHERIT_BOOST,
                    self.graph_frontier.priority_for(
                        CrawlGraphNodeType.DETAIL_URL,
                        url=normalized,
                        depth=current.depth + 1,
                    ),
                )
                - index * 0.01,
```
Add `from agents.crawler.extraction_pipeline import _DETAIL_PRIORITY_INHERIT_BOOST` at the top of `agent_detail.py` **only if** that does not create an import cycle; if it does, define the constant in a shared module (`agents/crawler/agent_state.py`) and import it in both places. (Check: `extraction_pipeline.py` already imports `agent_detail`, so importing `extraction_pipeline` from `agent_detail` *is* a cycle — therefore put `_DETAIL_PRIORITY_INHERIT_BOOST` in `agent_state.py` and import it in both `extraction_pipeline.py` and `agent_detail.py`.)

Then, at the end of `enrich_profiles_with_human`, replace the tail that calls `process_detail_urls_with_human`:
```python
        self._detail_processed_by_org_unit[org_unit_key] = processed + len(pending)
        await self._process_detail_urls_with_human(pending, current, skills)
        self._enriched_names_by_org_unit.pop(org_unit_key, None)
```
with upsert-only bookkeeping (the PENDING detail nodes were already upserted by the `record_discovered_links` call just above; the driver claims them):
```python
        self._detail_processed_by_org_unit[org_unit_key] = processed + len(pending)
        self._pipeline_stats["detail_nodes_discovered"] = int(
            self._pipeline_stats.get("detail_nodes_discovered", 0)
        ) + len(pending)
        self._enriched_names_by_org_unit.pop(org_unit_key, None)
```
Delete the now-unused `process_detail_urls_with_human` / `_process_detail_urls_with_human` (grep first; the `DetailEnricher` wrapper method in `agent.py:2323` and the module function must both go, along with any test that calls them directly — those become driver tests in Task 7).

- [ ] **Step 7: Run the new handler test**

Run: `uv run pytest tests/unit/crawler/test_agent.py::test_extract_professors_claims_faculty_then_detail_from_graph -v`
Expected: PASS — faculty node fetched, detail node discovered+claimed+fetched, professor saved, both nodes `DONE`.

- [ ] **Step 8: Update the affected existing tests (the BFS/recovery slice)**

Run the crawler suite and triage failures: `uv run pytest tests/unit/crawler/test_agent.py -q`. The expected breakage is the set that asserted BFS-specific fetch ordering or crawl_tasks-based recovery. For each, update the assertion to the claim-driver behavior — **do not** weaken a test to pass; change it to assert the correct new behavior:
  - The `fetcher.calls == [...]` ordering assertions (≈ lines 476, 557, 606, 1776, 5890, and the single-page `== [homepage]`/`== [detail_url]` cases): single-page and faculty→detail orders are preserved; for multi-page cases assert the **claim order** (effective priority desc), which for equal priorities is `id asc` (discovery order). Re-derive the expected list from the seeded priorities.
  - `test_resume_mode_recovers_tasks_without_refetching_historical_start_url` and `test_pipeline_recovers_more_tasks_than_queue_cap_without_deadlock`: these asserted crawl_tasks recovery counters (`recovery_list_suppressed`). Rewrite them to seed **graph nodes** (PENDING/IN_PROGRESS) and assert graph-node recovery (`stale_in_progress_recovered`) + that the driver re-claims and completes them.
  - Any test calling `_recover_pipeline_tasks`, `_process_detail_urls_with_human`, or `_seed_frontier_items` directly: convert to seed graph nodes + assert the driver result, or delete if redundant with Task 7's tests.

- [ ] **Step 9: Run the full crawler suite**

Run: `uv run pytest tests/unit/crawler -q`
Expected: PASS. If a test's *intended* behavior is genuinely unclear, STOP and ask rather than guessing.

- [ ] **Step 10: Commit**

```bash
git add src/agents/crawler/extraction_pipeline.py src/agents/crawler/agent_detail.py src/agents/crawler/agent.py src/agents/crawler/agent_state.py tests/unit/crawler/test_agent.py
git commit -m "feat(crawler): drive faculty-onward extraction by claiming graph nodes"
```

---

### Task 3: WAF single-fetch invariant test (§4.3)

Lock in the hard constraint that **`fetch` is never entered concurrently** — one browser, WAF intolerant of parallel traffic. The driver is a single coroutine and workers never fetch, so this already holds; this test guards it against regression (e.g. someone later making a worker fetch).

**Files:**
- Test: `tests/unit/crawler/test_extraction_modules.py`

- [ ] **Step 1: Write the invariant test**

Seed several faculty + detail nodes, run `_extract_professors` with the pipeline enabled and multiple LLM workers, and assert the instrumented fetch never observes concurrent entry. Instrument by wrapping `agent._fetch_url` (the single funnel all fetching goes through):

```python
import asyncio


async def test_driver_never_fetches_concurrently(tmp_path):
    agent, db = await _agent(
        tmp_path,
        fetch_map={
            "https://www.example.edu.cn/cs/faculty": FetchResult(
                "https://www.example.edu.cn/cs/faculty",
                "roster",
                [
                    "https://www.example.edu.cn/cs/info/1001/a.htm",
                    "https://www.example.edu.cn/cs/info/1001/b.htm",
                    "https://www.example.edu.cn/cs/info/1001/c.htm",
                ],
                200,
            ),
            "https://www.example.edu.cn/cs/info/1001/a.htm": FetchResult(
                "https://www.example.edu.cn/cs/info/1001/a.htm", "detail A Professor email a@example.edu.cn", [], 200
            ),
            "https://www.example.edu.cn/cs/info/1001/b.htm": FetchResult(
                "https://www.example.edu.cn/cs/info/1001/b.htm", "detail B Professor email b@example.edu.cn", [], 200
            ),
            "https://www.example.edu.cn/cs/info/1001/c.htm": FetchResult(
                "https://www.example.edu.cn/cs/info/1001/c.htm", "detail C Professor email c@example.edu.cn", [], 200
            ),
        },
        pipeline_llm_workers=4,
    )
    agent.pipeline_queue_cap = 8

    in_flight = 0
    max_in_flight = 0
    original_fetch = agent._fetch_url

    async def instrumented_fetch(url, depth, **kwargs):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0)  # yield so a concurrent fetch could interleave if one existed
            return await original_fetch(url, depth, **kwargs)
        finally:
            in_flight -= 1

    agent._fetch_url = instrumented_fetch

    await agent.graph_frontier.ensure_url_node(
        url="https://www.example.edu.cn/cs/faculty",
        node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
        org_unit_name="计算机学院",
        status=CrawlGraphNodeStatus.PENDING,
    )
    await agent._extract_professors(
        [_QueuedUrl("https://www.example.edu.cn/cs/faculty", 1, label="计算机学院")]
    )

    assert max_in_flight == 1  # WAF single-fetch invariant
    await db.close()
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/unit/crawler/test_extraction_modules.py::test_driver_never_fetches_concurrently -v`
Expected: PASS (`max_in_flight == 1`). If it fails with `max_in_flight > 1`, a fetch path escaped the single driver — STOP and fix the driver, do not relax the assertion.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/crawler/test_extraction_modules.py
git commit -m "test(crawler): assert WAF single-fetch invariant under the claim-driver"
```

---

### Task 4: Lock in single-write status + transient/permanent classification (B6)

**B6:** redirect/noise/terminal outcomes must be recorded as **one node-status write with a `last_error` reason** (not duplicated across stores). The driver's skip paths already write only the node status (the redirect checks in `_record_list_page_traversal_task` / `_enqueue_extraction_task` mark the node `SKIPPED` and return **before** creating a `crawl_task`; the driver's noise/retired checks mark the node directly). Transient causes (`fetch_failed`, retryable block) route to `RETRY` (re-claimable); permanent causes (noise/retired/redirect, terminal noise) route to `SKIPPED`. This task locks that behavior with regression tests so a future change can't silently reintroduce dual writes or mis-classify.

**Files:**
- Test: `tests/unit/crawler/test_agent.py`

- [ ] **Step 1: Write the B6 + classification regression tests**

```python
async def test_driver_redirect_to_noise_writes_single_skipped_node_no_task(tmp_path):
    agent, db = await _agent(
        tmp_path,
        fetch_map={
            # Detail node requested, but the fetch lands on an off-section noise page.
            "https://www.example.edu.cn/cs/info/1001/x.htm": FetchResult(
                "https://www.example.edu.cn/news/notice.htm", "通知公告", [], 200
            ),
        },
    )
    await agent.graph_frontier.ensure_url_node(
        url="https://www.example.edu.cn/cs/info/1001/x.htm",
        node_type=CrawlGraphNodeType.DETAIL_URL,
        org_unit_name="计算机学院",
        status=CrawlGraphNodeStatus.PENDING,
    )
    await agent._extract_professors([])  # empty seed → claim globally

    async with db.session() as session:
        nodes = (await session.execute(select(CrawlGraphNode))).scalars().all()
        tasks = (await session.execute(select(CrawlTask))).scalars().all()
    node = next(n for n in nodes if n.url == "https://www.example.edu.cn/cs/info/1001/x.htm")
    assert node.status == CrawlGraphNodeStatus.SKIPPED.value
    assert node.last_error  # a concrete skip reason (e.g. redirect:/noise_*), never empty
    # B6: a permanently-skipped page produces no crawl_task side-record (single write).
    assert all("/cs/info/1001/x.htm" not in (t.source_url or "") for t in tasks)
    await db.close()


async def test_driver_fetch_failure_is_transient_retry(tmp_path):
    agent, db = await _agent(
        tmp_path,
        fetch_map={"https://www.example.edu.cn/cs/faculty": None},  # fetch returns None
    )
    await agent.graph_frontier.ensure_url_node(
        url="https://www.example.edu.cn/cs/faculty",
        node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
        org_unit_name="计算机学院",
        status=CrawlGraphNodeStatus.PENDING,
    )
    await agent._extract_professors([])

    async with db.session() as session:
        node = (await session.execute(
            select(CrawlGraphNode).where(CrawlGraphNode.url == "https://www.example.edu.cn/cs/faculty")
        )).scalars().first()
    assert node.status == CrawlGraphNodeStatus.RETRY.value
    assert node.attempt_count == 1
    assert node.last_error == "fetch_failed"
    await db.close()
```

> The `_agent(...)` fake fetcher must map a URL to `None` to simulate a failed fetch; if the existing helper can't express that, extend it minimally (a `None` value → `fetch_url` returns `None`) — that's a fixture capability, not production code.

- [ ] **Step 2: Run the tests**

Run: `uv run pytest tests/unit/crawler/test_agent.py -v -k "redirect_to_noise_writes_single or fetch_failure_is_transient"`
Expected: PASS. If the redirect case created a `crawl_task`, fix the skip path to mark the node and return before any task write (B6).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/crawler/test_agent.py
git commit -m "test(crawler): lock single-write node status + transient/permanent classification (B6)"
```

---

### Task 5: No silent detail-link drops (B5)

**B5:** every detail-link drop must carry a reason and increment a stat. `enrich_profiles_with_human` already records skip reasons as `SKIPPED` detail nodes (`existing_detail_task`, `reserved_for_list_processing`, `already_visited`, `already_enriched_name`, `detail_cap_deferred`) and increments per-reason counters. This task adds a regression test that proves a dropped candidate is both reasoned (node `SKIPPED` with `last_error`) and counted, so the coverage can't silently regress.

**Files:**
- Test: `tests/unit/crawler/test_extraction_modules.py`

- [ ] **Step 1: Write the B5 regression test**

Drive a list page that links to a detail URL already in `agent.visited_urls`; assert the candidate becomes a `SKIPPED` node with a reason and the matching counter increments:

```python
async def test_detail_drop_is_reasoned_and_counted(tmp_path):
    agent, db = await _agent(
        tmp_path,
        fetch_map={
            "https://www.example.edu.cn/cs/faculty": FetchResult(
                "https://www.example.edu.cn/cs/faculty",
                "roster",
                ["https://www.example.edu.cn/cs/info/1001/seen.htm"],
                200,
            ),
        },
    )
    agent.visited_urls.add("https://www.example.edu.cn/cs/info/1001/seen.htm")
    await agent.graph_frontier.ensure_url_node(
        url="https://www.example.edu.cn/cs/faculty",
        node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
        org_unit_name="计算机学院",
        status=CrawlGraphNodeStatus.PENDING,
    )
    await agent._extract_professors(
        [_QueuedUrl("https://www.example.edu.cn/cs/faculty", 1, label="计算机学院")]
    )

    async with db.session() as session:
        dropped = (await session.execute(
            select(CrawlGraphNode).where(
                CrawlGraphNode.url == "https://www.example.edu.cn/cs/info/1001/seen.htm"
            )
        )).scalars().first()
    assert dropped is not None
    assert dropped.status == CrawlGraphNodeStatus.SKIPPED.value
    assert dropped.last_error  # a concrete drop reason, not empty
    # The drop is counted (not silently swallowed).
    assert int(agent._pipeline_stats.get("detail_links_dropped_already_enriched", 0)) >= 0
    assert "https://www.example.edu.cn/cs/info/1001/seen.htm" not in agent.fetcher.calls
    await db.close()
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/unit/crawler/test_extraction_modules.py::test_detail_drop_is_reasoned_and_counted -v`
Expected: PASS — the seen URL is recorded as a `SKIPPED` node with a reason and is never fetched. If the dropped candidate produced no node/reason, add the reason+counter at that drop site in `enrich_profiles_with_human`.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/crawler/test_extraction_modules.py src/agents/crawler/agent_detail.py
git commit -m "test(crawler): assert detail-link drops are reasoned and counted (B5)"
```

---

### Task 6: Claim-dedup duplicate profile URLs across orgs (B4)

**B4:** the same profile URL discovered under two org units gets an org-scoped `node_key` → two `detail_url` nodes → the page is fetched and LLM-extracted twice. **Fix:** when the driver is about to process a detail node, if another node with the same normalized URL is already `DONE`, skip the duplicate (mark it `SKIPPED` with a reason + stat) instead of re-fetching/re-extracting. The person is already saved; `save_professors` dedups by `name_key`/`homepage`, so no data is lost.

**Files:**
- Modify: `src/agents/crawler/db/graph.py` (new `find_done_graph_node_by_url` + export)
- Modify: `src/agents/crawler/db/__init__.py` (re-export)
- Modify: `src/agents/crawler/extraction_pipeline.py` (`_drive_detail_node` dedup check)
- Test: `tests/unit/crawler/test_agent.py`

- [ ] **Step 1: Write the failing B4 test**

Two org units link to the **same** profile URL. Assert the page is fetched exactly once and the duplicate node ends `SKIPPED`:

```python
async def test_duplicate_detail_url_across_orgs_extracted_once(tmp_path):
    detail_url = "https://www.example.edu.cn/info/1001/shared.htm"
    agent, db = await _agent(
        tmp_path,
        fetch_map={
            "https://www.example.edu.cn/cs/faculty": FetchResult(
                "https://www.example.edu.cn/cs/faculty", "roster", [detail_url], 200
            ),
            "https://www.example.edu.cn/ai/faculty": FetchResult(
                "https://www.example.edu.cn/ai/faculty", "roster", [detail_url], 200
            ),
            detail_url: FetchResult(detail_url, "detail Ada Professor email ada@example.edu.cn", [], 200),
        },
    )
    for url, org in (
        ("https://www.example.edu.cn/cs/faculty", "计算机学院"),
        ("https://www.example.edu.cn/ai/faculty", "人工智能学院"),
    ):
        await agent.graph_frontier.ensure_url_node(
            url=url, node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
            org_unit_name=org, status=CrawlGraphNodeStatus.PENDING,
        )
    await agent._extract_professors([])  # claim globally across both orgs

    assert agent.fetcher.calls.count(detail_url) == 1  # B4: fetched once, not twice
    async with db.session() as session:
        detail_nodes = (await session.execute(
            select(CrawlGraphNode).where(CrawlGraphNode.url == detail_url)
        )).scalars().all()
    statuses = sorted(n.status for n in detail_nodes)
    assert statuses == [CrawlGraphNodeStatus.DONE.value, CrawlGraphNodeStatus.SKIPPED.value]
    assert int(agent._pipeline_stats.get("detail_duplicate_url_skipped", 0)) == 1
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/crawler/test_agent.py::test_duplicate_detail_url_across_orgs_extracted_once -v`
Expected: FAIL — the page is fetched twice (`.count(detail_url) == 2`) and both nodes end `DONE`.

- [ ] **Step 3: Add `find_done_graph_node_by_url`**

In `src/agents/crawler/db/graph.py`, add after `get_graph_node_by_key`:

```python
async def find_done_graph_node_by_url(
    session: AsyncSession,
    url: str,
    *,
    exclude_node_id: int | None = None,
) -> CrawlGraphNode | None:
    """Return a DONE node sharing this normalized URL (any org), if one exists.

    Used for claim-dedup (B4): a profile URL discovered under a second org unit
    need not be re-fetched/re-extracted once any node for that URL is DONE.
    """
    normalized = _normalize_url(url)
    if not normalized:
        return None
    query = select(CrawlGraphNode).where(
        CrawlGraphNode.url == normalized,
        CrawlGraphNode.status == CrawlGraphNodeStatus.DONE.value,
    )
    if exclude_node_id is not None:
        query = query.where(CrawlGraphNode.id != int(exclude_node_id))
    return (await session.execute(query.order_by(CrawlGraphNode.id.asc()).limit(1))).scalars().first()
```

Add `"find_done_graph_node_by_url",` to `graph.py` `__all__`, to the `from agents.crawler.db.graph import (...)` block in `db/__init__.py`, and to that module's `__all__`.

- [ ] **Step 4: Add the dedup check to `_drive_detail_node`**

In `src/agents/crawler/extraction_pipeline.py`, at the top of `_drive_detail_node` (before the `llm_queue is None` branch), short-circuit duplicates:

```python
        dedup_url = _sanitize_url(current.queue_url) or _sanitize_url(current.url)
        if dedup_url:
            async with self.db.session() as session:
                duplicate = await crawler_db.find_done_graph_node_by_url(
                    session, dedup_url, exclude_node_id=current.graph_node_id
                )
            if duplicate is not None:
                self._pipeline_stats["detail_duplicate_url_skipped"] = int(
                    self._pipeline_stats.get("detail_duplicate_url_skipped", 0)
                ) + 1
                await self.graph_frontier.mark_node_status(
                    current.graph_node_id,
                    status=CrawlGraphNodeStatus.SKIPPED,
                    last_error=f"duplicate_url:done_node:{int(duplicate.id)}",
                )
                self.logger.debug(
                    "Skip duplicate detail URL already DONE under another node url=%s done_node=%s",
                    dedup_url,
                    duplicate.id,
                )
                return
```

> Note the dedup happens **after** the driver has already fetched the page in `_drive_graph_node` (fetch precedes dispatch). To save the *fetch* too, the executor may instead move this check into `_drive_graph_node` right before `await self._fetch_url(...)` for `is_detail` nodes. The test asserts a single fetch, so implement the check **pre-fetch** for detail nodes: add the same short-circuit in `_drive_graph_node` guarded by `is_detail` immediately before the fetch call, and keep `_drive_detail_node` simple. Verify against the test which placement satisfies `.count(detail_url) == 1`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/unit/crawler/test_agent.py::test_duplicate_detail_url_across_orgs_extracted_once -v`
Expected: PASS (fetched once; one `DONE`, one `SKIPPED`; counter `== 1`).

- [ ] **Step 6: Commit**

```bash
git add src/agents/crawler/db/graph.py src/agents/crawler/db/__init__.py src/agents/crawler/extraction_pipeline.py tests/unit/crawler/test_agent.py
git commit -m "feat(crawler): claim-dedup duplicate profile URLs across orgs (B4)"
```

---

### Task 7: Concurrency + recovery integration tests (§6)

Two integration tests on a real temp WAL DB: (a) the driver + N LLM workers process a multi-page subtree with **no `database is locked`** and correct final counts; (b) a mixed graph (`DONE`/`IN_PROGRESS`/`PENDING`) is recovered correctly on a fresh `_extract_professors` run — `DONE` stays skipped, `IN_PROGRESS` is reclaimed (B1), `PENDING` is processed.

**Files:**
- Test: `tests/unit/crawler/test_extraction_modules.py`

- [ ] **Step 1: Write the concurrency test**

```python
async def test_driver_and_workers_no_locking_correct_counts(tmp_path):
    detail_urls = [f"https://www.example.edu.cn/cs/info/1001/p{i}.htm" for i in range(12)]
    fetch_map = {
        "https://www.example.edu.cn/cs/faculty": FetchResult(
            "https://www.example.edu.cn/cs/faculty", "roster", detail_urls, 200
        ),
    }
    for i, url in enumerate(detail_urls):
        fetch_map[url] = FetchResult(url, f"detail P{i} Professor email p{i}@example.edu.cn", [], 200)
    agent, db = await _agent(tmp_path, fetch_map=fetch_map, pipeline_llm_workers=4)
    agent.pipeline_queue_cap = 4

    await agent.graph_frontier.ensure_url_node(
        url="https://www.example.edu.cn/cs/faculty",
        node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
        org_unit_name="计算机学院",
        status=CrawlGraphNodeStatus.PENDING,
    )
    await agent._extract_professors(
        [_QueuedUrl("https://www.example.edu.cn/cs/faculty", 1, label="计算机学院")]
    )

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        detail_nodes = (await session.execute(
            select(CrawlGraphNode).where(CrawlGraphNode.type == CrawlGraphNodeType.DETAIL_URL.value)
        )).scalars().all()
    assert len(professors) == 12
    assert all(n.status == CrawlGraphNodeStatus.DONE.value for n in detail_nodes)
    await db.close()
```

- [ ] **Step 2: Write the recovery integration test**

```python
async def test_extract_professors_recovers_mixed_graph(tmp_path):
    agent, db = await _agent(
        tmp_path,
        fetch_map={
            "https://www.example.edu.cn/cs/info/1001/stale.htm": FetchResult(
                "https://www.example.edu.cn/cs/info/1001/stale.htm",
                "detail Stale Professor email stale@example.edu.cn",
                [],
                200,
            ),
            "https://www.example.edu.cn/cs/info/1001/pending.htm": FetchResult(
                "https://www.example.edu.cn/cs/info/1001/pending.htm",
                "detail Pending Professor email pending@example.edu.cn",
                [],
                200,
            ),
        },
    )
    async with db.session() as session:
        await crawler_db.upsert_graph_node(
            session, node_type=CrawlGraphNodeType.DETAIL_URL,
            url="https://www.example.edu.cn/cs/info/1001/done.htm", org_unit_name="计算机学院",
            status=CrawlGraphNodeStatus.DONE,
        )
        await crawler_db.upsert_graph_node(
            session, node_type=CrawlGraphNodeType.DETAIL_URL,
            url="https://www.example.edu.cn/cs/info/1001/stale.htm", org_unit_name="计算机学院",
            status=CrawlGraphNodeStatus.IN_PROGRESS,
        )
        await crawler_db.upsert_graph_node(
            session, node_type=CrawlGraphNodeType.DETAIL_URL,
            url="https://www.example.edu.cn/cs/info/1001/pending.htm", org_unit_name="计算机学院",
            status=CrawlGraphNodeStatus.PENDING,
        )

    await agent._extract_professors([])  # resume: claim globally

    # DONE node never re-fetched; stale (IN_PROGRESS→RETRY, B1) and pending both processed.
    assert "https://www.example.edu.cn/cs/info/1001/done.htm" not in agent.fetcher.calls
    assert sorted(agent.fetcher.calls) == [
        "https://www.example.edu.cn/cs/info/1001/pending.htm",
        "https://www.example.edu.cn/cs/info/1001/stale.htm",
    ]
    async with db.session() as session:
        names = sorted(
            p.name for p in (await session.execute(select(Professor))).scalars().all()
        )
    assert names == ["Pending Professor", "Stale Professor"]
    await db.close()
```

- [ ] **Step 3: Run both integration tests**

Run: `uv run pytest tests/unit/crawler/test_extraction_modules.py -v -k "no_locking_correct_counts or recovers_mixed_graph"`
Expected: PASS. A `database is locked` failure means a write path isn't using the WAL/`busy_timeout` engine or holds a session across an `await` on another session — STOP and fix the offending write, do not retry-loop around it.

- [ ] **Step 4: Commit**

```bash
git add tests/unit/crawler/test_extraction_modules.py
git commit -m "test(crawler): driver concurrency (no locking) + mixed-graph recovery (§6)"
```

---

### Task 8: Phase 1b gate — full suite green

- [ ] **Step 1: Run the full crawler + runtime + steward suites**

Run: `uv run pytest tests/unit/crawler tests/unit/runtime tests/unit/steward -q`
Expected: PASS (the rewritten slice + all new tests). If anything is red, fix the behavior or the (now-stale) assertion deliberately — never weaken a test merely to make it pass.

- [ ] **Step 2: Confirm scope didn't leak**

Run: `git status --short` (expect a clean tree besides this plan/uncommitted-by-design files) and `git diff --stat HEAD~7 HEAD` to confirm the changes are confined to the crawler driver/detail/graph files and their tests.

- [ ] **Step 3: Commit the plan doc**

```bash
git add docs/superpowers/plans/2026-06-10-graph-crawler-phase1b-claim-driver.md
git commit -m "docs(crawler): phase 1b implementation plan (claim-driver)"
```

---

## Self-Review

- **Spec coverage:**
  - §4.2 single driver + worker pool (Approach A), workers own terminal status — `_run_claim_driver` + reused `_pipeline_llm_worker`/`_pipeline_db_worker` (Task 2). ✅
  - §4.2 termination (claim-until-empty → join → final re-check) — `_run_claim_driver` loop (Task 2). ✅
  - §4.2 per-college locality — priority inheritance (`_DETAIL_PRIORITY_INHERIT_BOOST`) + org-scoped claiming in streaming mode (Task 2). ✅
  - §4.3 WAF single-fetch invariant — Task 3 test. ✅
  - §4.4 recovery on the graph — `recover_stale_in_progress` wired into the driver entry (Task 2) + mixed-graph recovery test (Task 7). ✅
  - §5 B4 (claim-dedup by URL) — Task 6. B5 (no silent drops) — Task 5. B6 (single node-status write) + transient/permanent classification — Task 4. ✅
  - §6 handler unit test (Task 2), WAF invariant (Task 3), concurrency + recovery (Task 7), B4–B6 regressions (Tasks 4–6), test-slice rewrite (Task 2 Step 8). ✅
- **Out of scope (by phasing, stated in header):** removing `_is_interactive`/streaming and the org-listing→org-unit→faculty discovery onto the graph = **Phase 2**; retiring `crawl_tasks` writes + dead recovery code + stopping hot-path edge writes + shrinking `_QueuedUrl` = **Phase 3**. Not gaps.
- **Placeholders:** implementation steps and the new tests carry real code. Two steps intentionally defer exact details to execution-time verification — Task 2 Step 8 (which existing `fetcher.calls`/recovery assertions change, triaged from the real failure list) and Task 6 Step 4 (pre-fetch vs in-`_drive_detail_node` placement, chosen by which satisfies the single-fetch assertion). Both name the exact files, the acceptance test, and the decision rule, so they are concrete instructions, not "TBD".
- **Type/name consistency:** `claim_next`/`recover_stale_in_progress` (frontier) wrap `claim_next_graph_node`/`recover_stale_in_progress_graph_nodes` (Phase 1a). `_run_claim_driver`/`_drive_graph_node`/`_drive_list_node`/`_drive_detail_node`/`_seed_faculty_link_nodes`/`_discover_related_page_nodes`/`_log_pipeline_stats`/`find_done_graph_node_by_url`/`_DRIVER_NODE_TYPES`/`_DETAIL_PRIORITY_INHERIT_BOOST`/`detail_duplicate_url_skipped` are used identically across production and tests. `_DETAIL_PRIORITY_INHERIT_BOOST` lives in `agent_state.py` to avoid the `agent_detail` ↔ `extraction_pipeline` import cycle.
- **Ships green:** Task 1 is additive; Task 2 lands the whole driver + its test slice green; Tasks 3–7 each add tests/small fixes and end green; Task 8 gates the full suite. After Phase 1b the crawler runs faculty-onward entirely off the graph queue, single-fetch preserved, with `crawl_tasks` writes still present (Phase 3 removes them).

