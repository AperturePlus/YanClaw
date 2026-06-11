# Graph Crawler — Phase 1c: Detail Classification, Coverage & Encoding Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the faculty-onward claim-driver actually produce `professors` rows on real Chinese faculty sites whose profile pages use name-leaf URLs (e.g. SJTU CS `…/jiaoshiml/<pinyin>.html`), fix the per-page coverage cap that drops most professors, add a safety net so a misclassified-but-rich profile is never silently discarded, and repair the GBK↔UTF‑8 corruption introduced at the human-bridge HTTP boundary.

**Architecture:** Three independent, separately-shippable fixes on branch `neo`, on top of Phase 1b (claim-driver). (1) **Classification:** teach `_looks_like_profile_detail_url` to recognize faculty-section profile URLs so they become `detail_url` nodes (claimed + LLM-extracted) instead of traversal-only `faculty_followup_url` nodes (save-suppressed). This single lever also makes `extract_followup_faculty_links` stop scooping them (it already excludes profile-detail URLs). (2) **Safety net:** if a page reached via the list/followup path nonetheless parses as a single rich profile (reuse the existing `extract_detail_profile_record_from_snapshot`), save it instead of suppressing — defense in depth against future misclassification. (3) **Encoding:** force `charset=utf-8` on both ends of the human-bridge API (userscript POST header + aiohttp body read) so captured HTML is stored correctly; add a Python repair utility for already-cached mojibake.

**Tech Stack:** Python 3.11+, asyncio, SQLAlchemy 2.0 async, aiosqlite, aiohttp (human-bridge server), pytest + pytest-asyncio (`asyncio_mode=auto`), `uv` runner. Userscript: TypeScript + Vite (`userscripts/`).

**Source spec:** `docs/superpowers/specs/2026-06-10-graph-crawler-migration-design.md` — §4.1 (detail vs list node roles), §5 B5 (no silent drops). This plan is a **bug-fix phase (1c)**, ahead of spec Phase 2; it does not touch the streaming fork or org-discovery (Phase 2) or `crawl_tasks` retirement (Phase 3).

**Evidence (live run `logs/crawl_20260610_225714.log`, DB `data/universities/sjtu.edu.cn.db`):**
- `professors = 0`; **zero `detail_url` nodes**; `faculty_list_url`/`faculty_followup_url` nodes hold professor profile URLs.
- `Detail links filtered current=.../jiaoshiml.html kept=0 … dropped_directory=291` and `Followup links filtered … kept=291`.
- `Claim-driver stats … list_save_suppressed=38 detail_enqueued=0 … records_created=0 … followups=145 … llm_calls=1`.
- Roster `…/jiaoshiml.html` has 337 links, **290** of form `/jiaoshiml/<pinyin>.html`.
- Stored `link_signals_json` heading `'�˲���Ƹ'` → `.encode('utf-8').decode('gbk')` == `人才招聘` (lossless), confirming a GBK↔UTF‑8 boundary transcode, not browser/render corruption.

**Baseline:** branch `neo`. Run the suite green before starting: `uv run pytest tests/unit/crawler tests/unit/runtime -q` → expect all pass (record the exact count printed; use it to detect regressions).

---

## File-structure overview (what each task touches)

- `src/agents/crawler/agent_detail.py` — add `_looks_like_faculty_section_profile_url`; wire it into `_looks_like_profile_detail_url` (Task 1). Used as the safety-net detector via existing `extract_detail_profile_record_from_snapshot` (Task 3).
- `src/agents/crawler/extraction_pipeline.py` — add the list-page profile-rescue path in `_drive_list_node`/`_record_list_page_traversal_task` (Task 3); raise/parameterize the followup cap interplay (Task 4).
- `src/agents/crawler/fetchers/human_server.py` — force UTF‑8 body read in `_handle_complete` (Task 5).
- `userscripts/src/api.ts` — add `; charset=utf-8` to the request `Content-Type` (Task 5).
- `src/agents/crawler/fetchers/human_models.py` or a new `src/agents/crawler/text_repair.py` — `repair_mojibake_text` utility + apply on ingest (Task 5).
- Tests: `tests/unit/crawler/test_extraction_modules.py` (URL heuristics, rescue, encoding repair), `tests/unit/crawler/test_agent.py` (driver end-to-end, coverage).

---

### Task 1: Recognize faculty-section profile URLs as detail (root cause of bug #1)

Teach the central `_looks_like_profile_detail_url` predicate that a URL of the form `/<faculty-dir>/<name-leaf>.html` (e.g. `/jiaoshiml/duanshengxiong.html`) is a profile-detail URL. Because `extract_detail_profile_links` keys its keep/drop gate on this predicate, and `extract_followup_faculty_links` (`agent_parsing.py:51`) **excludes** profile-detail URLs, this one change reroutes such links from the traversal-only followup path to the detail path — without relying on (corrupted) anchor text.

**Files:**
- Modify: `src/agents/crawler/agent_detail.py` (add `_looks_like_faculty_section_profile_url`; extend `_looks_like_profile_detail_url` at `agent_detail.py:329`)
- Test: `tests/unit/crawler/test_extraction_modules.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/crawler/test_extraction_modules.py` (import the predicate at the top: `from agents.crawler.agent_detail import _looks_like_profile_detail_url`):

```python
import pytest
from agents.crawler.agent_detail import _looks_like_profile_detail_url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.cs.sjtu.edu.cn/jiaoshiml/duanshengxiong.html",
        "https://www.cs.sjtu.edu.cn/jiaoshiml/zhangzhuosheng.html",
        "https://example.edu.cn/szdw/lisiming.html",
        "https://example.edu.cn/info/1001/2002.htm",  # existing pattern still works
    ],
)
def test_faculty_section_profile_url_is_detail(url):
    assert _looks_like_profile_detail_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.cs.sjtu.edu.cn/jiaoshiml.html",   # the roster itself, not a profile
        "https://www.cs.sjtu.edu.cn/jiaoshiml/index.html",
        "https://www.cs.sjtu.edu.cn/jiaoshiml/list.html",
        "https://www.cs.sjtu.edu.cn/jiaoshiml/123.html",  # numeric = pagination/category
        "https://www.cs.sjtu.edu.cn/szdw.html",        # section landing, no name leaf
        "https://www.cs.sjtu.edu.cn/xygk.html",        # unrelated section
    ],
)
def test_non_profile_faculty_urls_are_not_detail(url):
    assert _looks_like_profile_detail_url(url) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/crawler/test_extraction_modules.py -v -k "faculty_section_profile_url_is_detail or non_profile_faculty_urls_are_not_detail"`
Expected: FAIL — the `/jiaoshiml/<name>.html` cases currently return `False`.

- [ ] **Step 3: Add the detector and wire it in**

In `src/agents/crawler/agent_detail.py`, add (after `_FACULTY_CATEGORY_STEMS`, near line 75) the faculty-section directory set and detector:

```python
# Directory segments that hold individual faculty profile pages (leaf = person slug).
_FACULTY_SECTION_DIRS = frozenset(
    {
        "jiaoshiml",
        "szdw",
        "szll",
        "jsdw",
        "rcdw",
        "shizi",
        "teacher",
        "teachers",
        "faculty",
        "people",
        "staff",
        "team",
        "tutor",
    }
    | _FACULTY_CATEGORY_STEMS
)
# Leaf stems that are landing/category pages, never an individual person.
_NON_PROFILE_LEAF_STEMS = frozenset({"index", "list", "default", "main", "more", "all"})


def _looks_like_faculty_section_profile_url(url: str) -> bool:
    """True for `/<faculty-dir>/<person-slug>.html` profile pages.

    SJTU CS and similar sites publish each professor at e.g.
    `…/jiaoshiml/duanshengxiong.html` — a faculty-section directory plus a
    pinyin name leaf. These are profile-detail pages, not list/followup pages.
    """
    parsed = urlparse((url or "").lower())
    path = parsed.path
    if not path.endswith((".htm", ".html", ".shtml")):
        return False
    segments = [seg for seg in path.split("/") if seg]
    if len(segments) < 2:
        return False
    parent = segments[-2]
    leaf = segments[-1].rsplit(".", 1)[0]
    if parent not in _FACULTY_SECTION_DIRS:
        return False
    if not leaf or leaf.isdigit():
        return False
    if leaf in _NON_PROFILE_LEAF_STEMS or leaf in _FACULTY_CATEGORY_STEMS:
        return False
    if leaf.endswith(("list", "index")):
        return False
    # Person slug: latin/pinyin (optionally with digits/underscore), e.g. "duanshengxiong", "lisiming2".
    return bool(re.fullmatch(r"[a-z][a-z0-9_]*", leaf))
```

Then extend `_looks_like_profile_detail_url` (currently at `agent_detail.py:329`) by adding the new check before the final `return`:

```python
def _looks_like_profile_detail_url(url: str) -> bool:
    lowered = (url or "").lower()
    if _is_query_profile_detail_url(lowered):
        return True
    if any(token in lowered for token in _CLEAR_PROFILE_DETAIL_HINTS):
        return True
    path = urlparse(lowered).path
    if re.search(r"/info/\d+/\d+(\.s?html?)?$", path):
        return True
    if _looks_like_faculty_section_profile_url(lowered):
        return True
    return False
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/crawler/test_extraction_modules.py -v -k "faculty_section_profile_url_is_detail or non_profile_faculty_urls_are_not_detail"`
Expected: PASS.

- [ ] **Step 5: Run the full crawler suite to check for classification regressions**

Run: `uv run pytest tests/unit/crawler -q`
Expected: PASS. If a test that asserted a `/jiaoshiml/<name>`-style URL was a *faculty/followup* link now fails, that test encoded the bug — update it to assert the URL is a detail link (do not weaken). If a genuinely non-profile URL now classifies as detail, tighten `_FACULTY_SECTION_DIRS`/`_NON_PROFILE_LEAF_STEMS` rather than the test.

- [ ] **Step 6: Commit**

```bash
git add src/agents/crawler/agent_detail.py tests/unit/crawler/test_extraction_modules.py
git commit -m "fix(crawler): classify /<faculty-dir>/<name>.html as profile detail (bug: 0 professors saved)"
```

---

### Task 2: Driver end-to-end — name-leaf profiles become saved professors (regression for bug #1)

Prove the whole flow on the real SJTU shape: a roster page links to a `…/jiaoshiml/<name>.html` profile; the driver must create a `detail_url` node, fetch it, extract, and save a `Professor` — with the list/roster page `DONE` (save-suppressed) and the profile node `DONE`.

**Files:**
- Test: `tests/unit/crawler/test_agent.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/crawler/test_agent.py` (mirror the existing `_agent(...)` fixture used by Phase 1b driver tests — adapt the call to the real helper signature at `test_agent.py:359` if it differs):

```python
async def test_namedleaf_profile_becomes_detail_node_and_saves(tmp_path):
    agent, db = await _agent(
        tmp_path,
        fetch_map={
            "https://www.cs.sjtu.edu.cn/jiaoshiml.html": FetchResult(
                "https://www.cs.sjtu.edu.cn/jiaoshiml.html",
                "faculty roster",
                ["https://www.cs.sjtu.edu.cn/jiaoshiml/duanshengxiong.html"],
                200,
            ),
            "https://www.cs.sjtu.edu.cn/jiaoshiml/duanshengxiong.html": FetchResult(
                "https://www.cs.sjtu.edu.cn/jiaoshiml/duanshengxiong.html",
                "Duan Shengxiong Professor email duan@cs.sjtu.edu.cn 研究方向 systems",
                [],
                200,
            ),
        },
    )
    await agent.graph_frontier.ensure_url_node(
        url="https://www.cs.sjtu.edu.cn/jiaoshiml.html",
        node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
        org_unit_name="计算机学院",
        status=CrawlGraphNodeStatus.PENDING,
    )
    await agent._extract_professors(
        [_QueuedUrl("https://www.cs.sjtu.edu.cn/jiaoshiml.html", 1, label="计算机学院")]
    )

    assert agent.fetcher.calls == [
        "https://www.cs.sjtu.edu.cn/jiaoshiml.html",
        "https://www.cs.sjtu.edu.cn/jiaoshiml/duanshengxiong.html",
    ]
    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
        nodes = {n.url: (n.type, n.status) for n in (await session.execute(select(CrawlGraphNode))).scalars().all()}
    assert len(professors) == 1
    assert nodes["https://www.cs.sjtu.edu.cn/jiaoshiml/duanshengxiong.html"] == (
        CrawlGraphNodeType.DETAIL_URL.value,
        CrawlGraphNodeStatus.DONE.value,
    )
    assert nodes["https://www.cs.sjtu.edu.cn/jiaoshiml.html"][1] == CrawlGraphNodeStatus.DONE.value
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails (before Task 1 is merged) or passes (after)**

Run: `uv run pytest tests/unit/crawler/test_agent.py::test_namedleaf_profile_becomes_detail_node_and_saves -v`
Expected after Task 1: PASS. If it FAILS because the profile node has type `faculty_followup_url`, Task 1's wiring did not take effect on the discovery path — verify `extract_detail_profile_links` keeps the link (add a temporary `agent._pipeline_stats` print or check the `detail_links` debug log).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/crawler/test_agent.py
git commit -m "test(crawler): name-leaf profile becomes detail node and saves a professor"
```

---

### Task 3: Safety net — rescue a rich profile reached via the list/followup path (decision: classify + safety net)

Even with Task 1, a future site could route a real profile through the list/followup path. Today `_record_list_page_traversal_task` (`extraction_pipeline.py:639`) unconditionally suppresses saves (`list_save_suppressed`) and marks the node `DONE`. Add a guarded rescue: before suppressing, run the existing `extract_detail_profile_record_from_snapshot` (`agent_detail.py:339`, conservative — requires a name + at least one of email/phone/research/bio and a profile-shaped URL) on the page; if it yields a record, save it via the normal save path and count `list_page_profile_rescued`.

**Files:**
- Modify: `src/agents/crawler/extraction_pipeline.py` (`_record_list_page_traversal_task`)
- Test: `tests/unit/crawler/test_extraction_modules.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit/crawler/test_extraction_modules.py`. Seed a node typed as `FACULTY_FOLLOWUP_URL` (the traversal path) whose page is actually a single rich profile, and assert a professor is saved and the rescue counter increments:

```python
async def test_list_path_rescues_rich_profile(tmp_path):
    profile_url = "https://www.cs.sjtu.edu.cn/jiaoshiml/duanshengxiong.html"
    agent, db = await _agent(
        tmp_path,
        fetch_map={
            profile_url: FetchResult(
                profile_url,
                "段圣雄 教授 个人简介 主要研究方向 systems 邮箱 duan@cs.sjtu.edu.cn " * 8,
                [],
                200,
            )
        },
    )
    # Force the traversal path: seed as a followup (list-type) node, not a detail node.
    await agent.graph_frontier.ensure_url_node(
        url=profile_url,
        node_type=CrawlGraphNodeType.FACULTY_FOLLOWUP_URL,
        org_unit_name="计算机学院",
        status=CrawlGraphNodeStatus.PENDING,
    )
    await agent._extract_professors([])  # claim globally

    async with db.session() as session:
        professors = (await session.execute(select(Professor))).scalars().all()
    assert len(professors) == 1
    assert int(agent._pipeline_stats.get("list_page_profile_rescued", 0)) == 1
    await db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/crawler/test_extraction_modules.py::test_list_path_rescues_rich_profile -v`
Expected: FAIL — currently the followup page is traversal-only; 0 professors saved.

- [ ] **Step 3: Add the rescue before suppression**

In `src/agents/crawler/extraction_pipeline.py`, inside `_record_list_page_traversal_task`, immediately **before** the `self._pipeline_stats["list_save_suppressed"]` increment (currently `:712`), insert the rescue. Reuse the already-imported `agent_detail` module and the existing save method used by the detail path (the DB-worker save call). Use the snapshot already computed at `:675` (`snapshot`):

```python
        rescued = agent_detail.extract_detail_profile_record_from_snapshot(
            fetched.text or snapshot, page_url=final_url
        )
        if rescued:
            saved = await self._save_profile_records([rescued], current, source_url=final_url)
            if saved:
                self._pipeline_stats["list_page_profile_rescued"] = int(
                    self._pipeline_stats.get("list_page_profile_rescued", 0)
                ) + 1
                await self.graph_frontier.mark_node_status(
                    current.graph_node_id,
                    status=CrawlGraphNodeStatus.DONE,
                    metadata={"crawl_task_id": int(row.id), "rescued_profile": True},
                )
                self.logger.info(
                    "Rescued rich profile from list/followup path url=%s name=%s",
                    final_url,
                    rescued.get("name"),
                )
                return "done"
```

> **Executor note:** `_save_profile_records` is a stand-in name for the pipeline's existing professor-save entry point used by the DB worker. Grep `extraction_pipeline.py` for the method that takes parsed records and calls `crawler_db.save_professors(...)` (look near `_pipeline_db_worker` / `_SaveEvent` handling) and call that, matching its real signature. If saving is only reachable via the DB queue, add a minimal direct-save helper that wraps `crawler_db.save_professors` in a `self.db.session()` and returns the saved count. Do not duplicate dedup logic — `save_professors` already dedups by `name_key`/`homepage`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/crawler/test_extraction_modules.py::test_list_path_rescues_rich_profile -v`
Expected: PASS — one professor saved, `list_page_profile_rescued == 1`.

- [ ] **Step 5: Confirm no false positives on genuine list pages**

Run: `uv run pytest tests/unit/crawler -q`
Expected: PASS. A genuine multi-professor roster must NOT be rescued — `extract_detail_profile_record_from_snapshot` already returns `None` when multiple labeled names are present (`_snapshot_has_multiple_labeled_names`, `agent_detail.py:353`). If a roster test now over-saves, that guard is the place to verify, not the test.

- [ ] **Step 6: Commit**

```bash
git add src/agents/crawler/extraction_pipeline.py tests/unit/crawler/test_extraction_modules.py
git commit -m "feat(crawler): rescue rich single profiles reached via the list/followup path (safety net)"
```

---

### Task 4: Coverage — ensure all profiles route through the uncapped detail path (bug #2)

After Task 1, profile links are detail links, so they no longer pass through `_FOLLOWUP_PAGE_LIMIT = 36` (`extraction_pipeline.py:46`, applied at `:415` only to *followup* links). Lock this in with a test: a roster linking to 60 name-leaf profiles must create 60 `detail_url` nodes, not 36. Also confirm the per-org-unit detail cap (`detail_profile_hard_cap_per_org_unit`, default 200) is the only remaining limiter and raise its default if 200 is too low for large departments.

**Files:**
- Test: `tests/unit/crawler/test_agent.py`
- Modify (only if the test shows truncation below 200): `src/agents/crawler/config.py:46`, `src/agents/crawler/agent.py:122`

- [ ] **Step 1: Write the failing/guard test**

```python
async def test_all_namedleaf_profiles_become_detail_nodes_not_capped_at_36(tmp_path):
    profiles = [f"https://www.cs.sjtu.edu.cn/jiaoshiml/p{i:03d}name.html" for i in range(60)]
    fetch_map = {
        "https://www.cs.sjtu.edu.cn/jiaoshiml.html": FetchResult(
            "https://www.cs.sjtu.edu.cn/jiaoshiml.html", "roster", profiles, 200
        )
    }
    for i, url in enumerate(profiles):
        fetch_map[url] = FetchResult(url, f"P{i} Professor email p{i}@cs.sjtu.edu.cn 研究方向 x", [], 200)
    agent, db = await _agent(tmp_path, fetch_map=fetch_map, pipeline_llm_workers=4)

    await agent.graph_frontier.ensure_url_node(
        url="https://www.cs.sjtu.edu.cn/jiaoshiml.html",
        node_type=CrawlGraphNodeType.FACULTY_LIST_URL,
        org_unit_name="计算机学院",
        status=CrawlGraphNodeStatus.PENDING,
    )
    await agent._extract_professors(
        [_QueuedUrl("https://www.cs.sjtu.edu.cn/jiaoshiml.html", 1, label="计算机学院")]
    )

    async with db.session() as session:
        detail_nodes = (await session.execute(
            select(CrawlGraphNode).where(CrawlGraphNode.type == CrawlGraphNodeType.DETAIL_URL.value)
        )).scalars().all()
    assert len(detail_nodes) == 60  # not truncated to the followup cap of 36
    await db.close()
```

- [ ] **Step 2: Run the test**

Run: `uv run pytest tests/unit/crawler/test_agent.py::test_all_namedleaf_profiles_become_detail_nodes_not_capped_at_36 -v`
Expected: PASS after Task 1 (detail discovery is uncapped up to the 200 per-org-unit cap). If it returns 36, a followup-cap path is still capturing profiles — re-check Task 1's predicate against these synthetic URLs (`p000name` is a valid slug under `jiaoshiml`).

- [ ] **Step 3 (conditional): Raise the per-org-unit hard cap default**

Only if real departments exceed 200 profiles (SJTU CS roster ≈ 290): change the default in both places to a higher bound and keep it configurable.

`src/agents/crawler/config.py:46`:
```python
    detail_profile_hard_cap_per_org_unit: int = Field(default=400, ge=1)
```
`src/agents/crawler/agent.py:122`:
```python
        detail_profile_hard_cap_per_org_unit: int = 400,
```

- [ ] **Step 4: Run the crawler suite**

Run: `uv run pytest tests/unit/crawler -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/crawler/test_agent.py src/agents/crawler/config.py src/agents/crawler/agent.py
git commit -m "fix(crawler): route all profiles through uncapped detail path; raise per-org detail cap (bug: not every professor crawled)"
```

---

### Task 5: Fix the GBK↔UTF‑8 corruption at the human-bridge boundary (bug #3)

The captured DOM is correct Unicode (browser renders fine), but stored text/anchors are GBK bytes decoded as UTF‑8 (heading `'�˲���Ƹ'` → `人才招聘` via `.encode('utf-8').decode('gbk')`, lossless). The userscript POSTs `Content-Type: application/json` with **no charset** (`api.ts:23`); the aiohttp server reads `await request.json()` (`human_server.py:75`). Force UTF‑8 on both ends, add a defensive repair utility for any already-mojibake text, and verify with a real captured sample.

**Files:**
- Create: `src/agents/crawler/text_repair.py`
- Modify: `src/agents/crawler/fetchers/human_server.py` (`_handle_complete`)
- Modify: `userscripts/src/api.ts` (request `Content-Type`)
- Test: `tests/unit/crawler/test_extraction_modules.py`

- [ ] **Step 1: Reproduce & pin the boundary (diagnosis — do this first)**

Run the human-bridge server locally and POST a known GBK-origin string to confirm which side mis-transcodes. From the repo root:

```bash
uv run python - <<'PY'
# What aiohttp does with a UTF-8 JSON body that has NO charset in Content-Type.
import asyncio, json
from aiohttp import web
async def h(request):
    raw = await request.read()
    body_json = await request.json()           # current code path
    forced = json.loads(raw.decode("utf-8"))    # forced-utf8 path
    return web.json_response({
        "charset": request.charset,
        "json_html": body_json["html"],
        "forced_html": forced["html"],
        "raw_is_utf8": raw.decode("utf-8", "strict") is not None,
    })
async def main():
    app = web.Application(); app.router.add_post("/c", h)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 21599); await site.start()
    import aiohttp
    sample = "人才招聘 段圣雄 教授"
    async with aiohttp.ClientSession() as s:
        # No charset (mirrors the userscript), body encoded UTF-8 on the wire:
        r = await s.post("http://127.0.0.1:21599/c",
                         data=json.dumps({"html": sample}).encode("utf-8"),
                         headers={"Content-Type": "application/json"})
        print(await r.json())
    await runner.cleanup()
asyncio.run(main())
PY
```
Record whether `json_html` comes back correct or mojibake, and `charset`. (Hypothesis: `request.charset is None`; aiohttp may fall back to a non-UTF‑8 default on this platform, corrupting `json_html` while `forced_html` is correct.) This pins whether the server read is the culprit. Either way the fix below (force UTF‑8 read + send charset header) is correct and idempotent.

- [ ] **Step 2: Write the failing repair-utility test**

Add to `tests/unit/crawler/test_extraction_modules.py`:

```python
from agents.crawler.text_repair import repair_mojibake_text


def test_repair_mojibake_recovers_gbk_as_utf8():
    # Real captured corruption: GBK bytes decoded as UTF-8.
    broken = "人才招聘".encode("gbk").decode("utf-8")
    assert broken != "人才招聘"
    assert repair_mojibake_text(broken) == "人才招聘"


def test_repair_mojibake_leaves_clean_text_untouched():
    clean = "段圣雄 教授 Professor email duan@cs.sjtu.edu.cn"
    assert repair_mojibake_text(clean) == clean


def test_repair_mojibake_leaves_pure_ascii_untouched():
    assert repair_mojibake_text("Ada Professor email ada@example.edu.cn") == "Ada Professor"[:0] + "Ada Professor email ada@example.edu.cn"
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/unit/crawler/test_extraction_modules.py -v -k repair_mojibake`
Expected: FAIL — `ModuleNotFoundError: agents.crawler.text_repair`.

- [ ] **Step 4: Create the repair utility**

Create `src/agents/crawler/text_repair.py`:

```python
"""Repair GBK-bytes-decoded-as-UTF-8 mojibake (human-bridge boundary corruption).

A page's correct Unicode text was encoded to GBK bytes and then decoded as
UTF-8, producing reversible mojibake (e.g. ``人才招聘`` -> ``人`` ...).
`repair_mojibake_text` reverses that transcode only when it is confidently
recoverable, and otherwise returns the input unchanged.
"""

from __future__ import annotations

import re

# CJK presence after a successful re-decode signals a genuine recovery.
_CJK_RE = re.compile(r"[一-鿿]")
# Characters typical of GBK-as-UTF-8 mojibake (Latin-1 supplement / CJK-compat bytes).
_MOJIBAKE_HINT_RE = re.compile(r"[À-ÿŒ-ƒʰ-˿]")


def repair_mojibake_text(text: str) -> str:
    if not text:
        return text
    # Already contains real CJK -> not corrupted in this way.
    if _CJK_RE.search(text):
        return text
    # No mojibake-shaped characters -> nothing to do (plain ASCII/Latin stays put).
    if not _MOJIBAKE_HINT_RE.search(text):
        return text
    try:
        recovered = text.encode("utf-8").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    # Only accept the recovery if it actually produced CJK text.
    if _CJK_RE.search(recovered):
        return recovered
    return text
```

> The third assertion in Step 2 reduces to `repair_mojibake_text("Ada Professor email ada@example.edu.cn")` returning the same string — pure ASCII has no mojibake hint, so it is returned unchanged.

- [ ] **Step 5: Run the repair-utility test to verify it passes**

Run: `uv run pytest tests/unit/crawler/test_extraction_modules.py -v -k repair_mojibake`
Expected: PASS.

- [ ] **Step 6: Force UTF‑8 read on the server + apply repair on ingest**

In `src/agents/crawler/fetchers/human_server.py`, replace the body read in `_handle_complete` (`:74-81`) to force UTF‑8 and repair defensively. Add the import at the top: `from agents.crawler.text_repair import repair_mojibake_text`.

```python
    try:
        raw_body = await request.read()
        body = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, Exception):
        return _json_response({"error": "invalid json"}, status=400)

    html = repair_mojibake_text(body.get("html", ""))
    if not html:
        return _json_response({"error": "html is required"}, status=400)
```

- [ ] **Step 7: Send an explicit charset from the userscript**

In `userscripts/src/api.ts`, set the charset on both request helpers so the wire contract is unambiguous. Change both occurrences of:
```ts
      headers: { 'Content-Type': 'application/json' },
```
to:
```ts
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
```
(Lines `api.ts:23` and `api.ts:41`.) Then rebuild the userscript bundle:
```bash
cd userscripts && npm run build && cd ..
git diff --stat userscripts/dist/yanclaw-assistant.user.js
```
Expected: `userscripts/dist/yanclaw-assistant.user.js` regenerated with the new header. (If `npm` is unavailable in this environment, note it and hand-apply the same two-line change to the `dist` file's `headers` objects; the build is a packaging step, not logic.)

- [ ] **Step 8: Run the full suite**

Run: `uv run pytest tests/unit/crawler tests/unit/runtime -q`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/agents/crawler/text_repair.py src/agents/crawler/fetchers/human_server.py userscripts/src/api.ts userscripts/dist/yanclaw-assistant.user.js tests/unit/crawler/test_extraction_modules.py
git commit -m "fix(crawler): force utf-8 at human-bridge boundary + repair GBK mojibake (bug: corrupted Chinese text)"
```

---

### Task 6: Phase 1c gate — full suite green + live re-crawl validation

- [ ] **Step 1: Run the full unit suites**

Run: `uv run pytest tests/unit/crawler tests/unit/runtime tests/unit/steward -q`
Expected: PASS (baseline count from the plan header + the new tests). Never weaken a test to pass; fix the behavior or update a stale assertion deliberately.

- [ ] **Step 2: Confirm scope didn't leak**

Run: `git status --short` and `git diff --stat <baseline-commit> HEAD`. Expect changes confined to: `agent_detail.py`, `extraction_pipeline.py`, `config.py`, `agent.py`, `fetchers/human_server.py`, `text_repair.py`, `userscripts/src/api.ts` (+ rebuilt dist), and crawler tests.

- [ ] **Step 3: Live validation against SJTU CS (manual, human-bridge)**

Fresh-crawl just the CS college and verify the bug is gone. After the run:
```bash
uv run python - <<'PY'
import sqlite3
con = sqlite3.connect("data/universities/sjtu.edu.cn.db"); cur = con.cursor()
print("professors:", cur.execute("select count(*) from professors").fetchone()[0])
print("detail nodes by status:",
      cur.execute("select status,count(*) from crawl_graph_nodes where type='detail_url' group by status").fetchall())
row = cur.execute("select text_snapshot from crawl_page_cache where url like '%jiaoshiml/%' limit 1").fetchone()
print("sample text has real CJK:", bool(row) and any('一' <= ch <= '鿿' for ch in (row[0] or "")))
con.close()
PY
```
Expected: `professors` is in the hundreds (CS roster ≈ 290), `detail_url` nodes exist and are mostly `done`, and cached text contains real CJK (no mojibake). If `professors` is still 0, STOP and re-open Phase 1 investigation — do not ship.

- [ ] **Step 4: Commit the plan doc**

```bash
git add docs/superpowers/plans/2026-06-11-graph-crawler-phase1c-detail-classification-coverage-encoding.md
git commit -m "docs(crawler): phase 1c plan (detail classification, coverage, encoding)"
```

---

## Self-Review

- **Spec/issue coverage:**
  - Bug #1 (0 professors) — root cause is detail-link misclassification → Task 1 (predicate fix) + Task 2 (end-to-end regression). The detail-only-policy interaction gets a safety net in Task 3. ✅
  - Bug #2 (not every professor) — followup-cap funneling → Task 4 (uncapped detail routing + raised per-org cap). ✅
  - Bug #3 (encoding) — GBK↔UTF‑8 boundary transcode → Task 5 (force UTF‑8 read + charset header + repair utility), reproduction-first. ✅
  - Spec §5 B5 (no silent drops) — Task 3 turns a silent suppression into a counted rescue (`list_page_profile_rescued`). ✅
- **Out of scope (by phasing):** removing the `_is_interactive` streaming fork + org-listing/org-unit discovery onto the graph = **Phase 2**; retiring `crawl_tasks`/edge writes = **Phase 3**. The 50 `pending` `org_unit` nodes are expected (run was scoped to CS); not a bug addressed here.
- **Placeholder scan:** Task 3 Step 3 names `_save_profile_records` as a stand-in and gives an explicit grep+adapt rule for the real save entry point (the only deferred specific, with a concrete decision rule). Task 4 Step 3 and Task 5 Step 7 are conditional/packaging steps with exact edits. All test code is concrete.
- **Type/name consistency:** `_looks_like_faculty_section_profile_url`, `_FACULTY_SECTION_DIRS`, `_NON_PROFILE_LEAF_STEMS`, `repair_mojibake_text`, `list_page_profile_rescued`, `list_save_suppressed` are used identically across tasks. `_looks_like_profile_detail_url` remains the single classification lever consumed by both `extract_detail_profile_links` (keep gate) and `extract_followup_faculty_links` (exclude gate).
- **Ships green:** Task 1 is additive + reroutes classification; Task 2 proves the flow; Tasks 3–5 each add a guarded fix + tests and end green; Task 6 gates unit suites and requires a live re-crawl showing professors > 0 before shipping.
```
