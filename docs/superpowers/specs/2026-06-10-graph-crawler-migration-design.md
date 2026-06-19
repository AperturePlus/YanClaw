# Graph-Based Crawler — Completing the Queue→Graph Migration

- **Date:** 2026-06-10
- **Status:** Design approved (brainstorming); pending implementation plan
- **Scope:** Complete the partial queue→graph migration of `src/agents/crawler`, fold in enhancements, and fix latent bugs in the graph layer.

---

## 1. Context & Problem

The crawler was migrated from a queue-based architecture to a graph-based one, but **the migration is incomplete**: the graph was layered *on top of* the legacy queue rather than replacing it.

Symptoms in the current code:

- **Five representations of "a thing to crawl"** coexist: `crawl_graph_nodes` (new), `GraphFetchCandidate`, `_QueuedUrl` (legacy, with `graph_node_id/graph_node_type/graph_priority_score` *bolted on*), `crawl_tasks` (legacy persistent queue), and `_ExtractionTaskItem` (also with `graph_node_id` bolted on).
- **Dual bookkeeping in lockstep:** every task transition writes **both** `crawl_tasks` status **and** graph-node status (e.g. `extraction_pipeline.py:1424-1435`, `:1521-1537`, `_enqueue_extraction_task` `:857-937`). Two persistent state machines kept in sync by hand.
- **The graph does not actually schedule.** `next_fetch_candidates()` is read only at *seed time* (`agent.py:514`, `:674`, `extraction_pipeline.py:211`); the live work queue is an in-memory `pages_to_process` BFS over `_QueuedUrl`. So the persisted frontier and the in-memory frontier are two sources of truth that can diverge.
- **Edges are written but never read.** `crawl_graph_edges` is written on the hot path (`upsert_graph_edge`, often 2 edges/discovery) but no scheduling/decision path ever reads it — pure write amplification.
- **The DB engine uses bare defaults** (`runtime/database.py:35`): no WAL, no `busy_timeout`, no pragma tuning, while concurrent writers (driver + 4 LLM workers + DB worker) hit one SQLite file per university.

This design completes the migration so the **graph is the single source of truth**, removes the duplicated machinery, and fixes the bugs that the half-migration left behind.

---

## 2. Goals & Non-Goals

**Goals**
- Make `crawl_graph_nodes` the authoritative frontier *and* work queue for the **entire** pipeline (org-listing → org-unit → faculty-list → pagination/followup → detail).
- One state machine, one recovery path, no dual bookkeeping.
- Preserve the hard **WAF single-fetch** constraint and current throughput shape (serial fetch, concurrent LLM extraction).
- Fix the latent graph-layer bugs (B1–B6 below).

**Non-Goals**
- No change to the harvested data model (`professors`, `academicians`, `professor_affiliations`, org units) beyond what dedup/attribution requires.
- No backfill of legacy `crawl_tasks` into nodes — resume re-derives the frontier (see §4 decision D2).
- No new fetcher backends; no change to the human-assisted browser bridge protocol.
- No speculative use of the edge graph (kept dormant for possible future steward/provenance use).

---

## 3. Approved Decisions (from brainstorming)

| ID | Decision |
|----|----------|
| D1 | **Graph = single source of truth.** `crawl_graph_nodes` is the frontier + work queue; `crawl_tasks` is demoted. |
| D2 | **Re-derive frontier on resume.** No legacy backfill; rebuild PENDING nodes from `org_units` + `crawl_page_cache` + retryable failures. Professors + page cache are kept. |
| D3 | **Consumption = durable backlog + worker pool** (Approach A): a single driver claims nodes and feeds the existing LLM/DB worker pool via a bounded `asyncio.Queue`. |
| D4 | **Full pipeline on the graph.** Every stage is a node handler; discovery just upserts PENDING nodes. Delivered phased (faculty-onward first). |
| D5 | **Retire `crawl_tasks` from the write path** and **stop writing `crawl_graph_edges` on the hot path** (table kept dormant). |
| D6 | **WAF single-fetch is an invariant:** fetch concurrency is exactly 1 (one browser); only LLM extraction + DB writes are concurrent. |

---

## 4. Architecture

### 4.1 Node model & lifecycle

Every fetchable thing is **one graph node = one unit of work**. A node's `type` selects its handler; its `status` is the single authoritative lifecycle.

**Status state machine:**

| status | meaning | claimable? |
|---|---|---|
| `PENDING` | newly discovered | ✅ |
| `IN_PROGRESS` | claimed by the driver (lease) | no |
| `RETRY` | transient failure (fetch/timeout/invalid-json/rich-no-data) | ✅ |
| `DONE` | processed successfully | no |
| `FAILED` | permanent failure (retries exhausted, hard reject) | no (terminal) |
| `SKIPPED` | intentionally skipped (noise/redirect/retired) | no (terminal) |

A node stores everything scheduling needs: `type`, `url`, org attribution (`org_unit_name`/`id`), `base_priority` + `attempt_count` (see B3), `depth`, `confidence`, `last_error`, and small `metadata_json` (`fetch_action`, `identity_url`, result counts). **It does not store page text.**

**Where retired `crawl_tasks` responsibilities go:**
- **Page snapshot** → `crawl_page_cache` (already round-trips a full `FetchResult`: text + links + link_signals + block_reason — `db/page_cache.py:61-85`). The LLM worker re-reads + recompacts from there; crash-safe for re-extraction.
- **Dedup** → `node_key` uniqueness + node status (a `DONE` node is never re-claimed).
- **Failures** → existing `crawl_extraction_failures` table.
- **Recovery** → node status (§4.4).

`_QueuedUrl` loses its bolted-on `graph_node_*` fields and becomes a thin read-view over a node (or is removed); `_ExtractionTaskItem` keeps only what the LLM worker needs, built from `node + page_cache`.

### 4.2 Claim/consume loop & worker pool (Approach A)

**One driver coroutine owns all claiming and all fetching.** A single claimer makes claiming atomic by construction (fixes the non-atomic-claim bug). Each claim is one write transaction: select `PENDING`/`RETRY` ordered by `(effective_priority desc, confidence desc, depth asc, attempt_count asc, id asc)`, flip to `IN_PROGRESS`, return the row.

**Node-type handlers** (the old stage functions, re-pointed at the graph):

| node type | handler does | children upserted (PENDING) |
|---|---|---|
| `org_listing_url` | fetch → LLM extract org units | `org_unit`, more `org_listing_url` |
| `org_unit` | fetch → find faculty pages (heuristics + LLM fallback) | `faculty_list_url` (or `SKIPPED` no-faculty) |
| `faculty_list_url` / `pagination_url` / `followup_url` | fetch → discover pagination/followups + detail links | `pagination_url`, `followup_url`, `detail_url` |
| `detail_url` | fetch (driver) → **enqueue LLM job** → leave `IN_PROGRESS` | — (worker marks terminal) |

List pages remain traversal-only (no professor save); professor extraction happens on `detail_url` nodes — preserving current behavior.

**Workers do LLM-only.** The driver puts `(node_id, FetchResult)` jobs onto a bounded `asyncio.Queue`; the LLM-worker pool (default 4) extracts, the DB-worker (default 1) saves, then **the worker marks the node `DONE`/`RETRY`/`FAILED`**. The bounded queue provides backpressure (a full queue blocks the driver's `put`, pacing the serial fetch). Workers never fetch (D6).

**Termination:** driver claims until none are claimable, then `await llm_queue.join()` + `db_queue.join()`, then one final claim re-check (workers create no navigation nodes, so the backlog cannot grow afterward) → stop.

**Streaming/batch convergence:** the `_is_interactive` fork is removed. Both modes upsert nodes and the driver consumes uniformly. Per-college locality (human-assisted "one college at a time") is preserved via **priority inheritance**: a processed `org_unit`'s `faculty_list` children get a priority boost, and their `detail` children likewise, so a college's subtree is claimed before moving on.

### 4.3 WAF single-fetch invariant

Fetch concurrency is exactly 1: one browser, WAF intolerant of parallel/automated traffic. All fetching is serialized through the driver; only the LLM call + DB save run concurrently. Enforced by code structure and by a test that asserts `fetch` is never re-entered concurrently.

### 4.4 Recovery / resume (one path, on the graph)

- **Run start:** `recover_stale_in_progress_graph_nodes()` flips orphaned `IN_PROGRESS` → `RETRY` (the missing analogue of `recover_stale_in_progress_crawl_tasks`).
- **Re-derive frontier (D2):** on resume, re-seed root + re-derive `PENDING` nodes from existing `org_units` + retryable fetch-failure URLs; existing `DONE` nodes stay skipped; `page_cache` makes re-fetch cheap; professors untouched.
- **Remove** `list_recoverable_crawl_tasks` / `recover_stale_in_progress_crawl_tasks` and the crawl_tasks-based resume branches (`_resume_without_start_page`, etc.).
- **Crash safety:** any mid-flight node is `IN_PROGRESS` → reset to `RETRY` on next run → re-claimed. No orphans, no divergence (single store).

### 4.5 Concurrency foundation (prerequisite — must land first)

Even with single-fetch there are concurrent *writers* (driver claim/upsert + workers' terminal writes + DB-worker saves). The SQLite engine (`runtime/database.py`) gains, on connect:
- `PRAGMA journal_mode=WAL`, `busy_timeout=15000`, `synchronous=NORMAL`, `foreign_keys=ON`.
- Related writes batched into single transactions (claim+stamp; save+mark-DONE) instead of session-per-mutation.
- Retiring `crawl_tasks` + dual-status writes roughly halves write volume.

---

## 5. Bug-Fix Register

| # | Bug | Evidence | Fix |
|---|---|---|---|
| B1 | Stale `IN_PROGRESS` graph nodes never recovered | no analogue of `recover_stale_in_progress_crawl_tasks` | reset `IN_PROGRESS`→`RETRY` at run start (§4.4) |
| B2 | `FAILED`==`DONE` rank → re-discovered FAILED node never re-queued | `db/graph.py:419-428`, `:132` | give terminal states their own rank; let higher-confidence re-discovery lift a non-`DONE` node → `RETRY` |
| B3 | Attempt-backoff erased by re-discovery | priority only raised `graph.py:135` vs `−5` at `:232` | store `base_priority` + `attempt_count` separately; compute effective order priority at claim time |
| B4 | Same profile URL extracted twice across orgs (wasted LLM) | org-scoped `node_key` → 2 nodes/URL | claim-dedup by normalized URL; record the 2nd org's affiliation cheaply (save dedups the person via `name_key`/`homepage`) |
| B5 | Silent detail-link drops, no counter | `agents.md:144` | every drop returns a reason + increments a stat |
| B6 | Redirect/noise/terminal recorded in two stores | dual crawl_task + node writes | one node-status write with `last_error` reason |

Also: a transient-vs-permanent failure classification pass so transient causes route to `RETRY` (re-claimable) and only permanent causes route to `FAILED`/`SKIPPED` (ties into B2).

---

## 6. Testing Strategy (TDD — failing test before each change)

- **Invariant test:** a fetcher stub that asserts `fetch` is never entered concurrently (WAF single-thread guarantee).
- **Handler unit tests:** `org_listing→org_unit`, `org_unit→faculty_list`, `faculty_list→detail`, `detail→save`; status transitions; claim ordering + atomicity.
- **Recovery test:** mixed graph (DONE/IN_PROGRESS/PENDING) → resume reclaims correctly; re-derive from `org_units` + `page_cache`.
- **Concurrency test:** driver + N workers on a temp WAL DB → no "database is locked", correct final counts.
- **Bug regression tests:** one per B1–B6.
- **Expected cost:** `tests/unit/crawler/test_agent.py` + `test_db.py` lean heavily on `crawl_tasks` semantics and edge assertions; a substantial slice will be rewritten as part of this work.

---

## 7. Phasing (each phase ships green; system runnable between phases)

- **Phase 0 — Foundation:** WAL/`busy_timeout`/`synchronous` pragmas + transaction batching. Independently shippable; fixes locking immediately.
- **Phase 1 — Graph-as-queue, faculty-onward:** convert `_extract_professors` to claim-from-graph + single driver + worker pool; workers own terminal status; add stale-node recovery; land B1–B6. (Highest ROI — volume + bugs live here.)
- **Phase 2 — Discovery on graph:** org-listing / org-unit / faculty-finding become node handlers; remove the `_is_interactive` streaming fork and the in-memory BFS.
- **Phase 3 — Cleanup:** retire `crawl_tasks` writes + dead recovery code; stop hot-path edge writes; reduce `_QueuedUrl` to a thin node view; remove bolted-on `graph_node_*` fields.

---

## 8. Risks & Mitigations

- **Behavior change: priority-ordered vs hard-sequential streaming.** Mitigation: tune priority inheritance so a college's subtree is claimed contiguously; validate against a human-assisted run.
- **Large test rewrite.** Mitigation: phased delivery keeps each step green; write new tests first.
- **Resume re-walks some frontier (D2).** Acceptable by decision; `page_cache` keeps re-fetch cheap and professors are preserved.
- **Edge table left dormant.** Mitigation: keep `upsert_graph_edge` + schema; document that it is intentionally unused on the hot path.

---

## 9. Open Questions

None blocking. Edge-graph provenance (if the steward later wants it) is deferred, not designed here.
