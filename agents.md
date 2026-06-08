# Yanclaw Agents — 开发者文档

本文件面向后续开发者，覆盖 `src/agents/` 下两个智能体（`crawler` 与 `data_steward`）的内部架构、运行流程、关键模块、配置与调试线索。设计与需求的高层文档参见 `docs/crawler/`，本文聚焦"代码层面要看什么、改什么、怎么排错"。

---

## 1. 顶层结构

```
src/
├── runtime/                  共享运行时原语（不放业务编排）
│   ├── llm.py                LLMClient: chat + tool calling 循环
│   ├── skills.py             SkillManager: 加载/版本/回滚 skill
│   ├── context.py            ContextManager: token 预算 + 分块 + 文本压缩
│   ├── database.py           DatabaseManager (async SQLAlchemy)
│   ├── logger.py             setup_logging + get_logger
│   ├── crawler_cli.py        Click CLI 入口（被 `yanclaw` 入口加载）
│   └── skills/               共享元 skills (create-skills.md 等)
└── agents/
    ├── crawler/              智能爬虫
    │   ├── agent.py          状态机 + 流水线（最大模块, ~2400 行）
    │   ├── agent_detail.py   人工辅助模式下的 detail 页提取/筛选
    │   ├── agent_parsing.py  JSON / 链接 / 分页解析辅助
    │   ├── dispatcher.py     CrawlDispatcher，每所大学一个 Agent
    │   ├── config.py         CrawlerSettings (pydantic-settings)
    │   ├── url_heuristics.py URL 评分、faculty/detail/noise 判定（核心启发式）
    │   ├── tools.py          LLM 可调用工具（save_professors / extract_links）
    │   ├── sanitizer.py      Professor payload 规范化、退休/退役过滤
    │   ├── cookies.py        per-domain cookie 文件
    │   ├── models.py         ORM 模型（每所大学一个 SQLite DB）
    │   ├── fetchers/
    │   │   ├── httpx_fetcher.py  公共 helper（filter_same_domain/html_to_text/extract_links/WAF 检测）
    │   │   ├── human_bridge.py   人工浏览器桥（aiohttp 服务 + Tampermonkey 配合）
    │   │   ├── human_server.py   /api/* HTTP 接口
    │   │   ├── human_models.py   FetchJob / JobQueue / DecisionRequest
    │   │   └── link_signals.py   含 anchor/heading/parent context 的链接抽取
    │   └── db/
    │       ├── professors.py     upsert_professor / upsert_academician / 关联表
    │       ├── tasks.py          crawl_tasks 流水线持久化
    │       ├── university.py     university_meta / org_units / crawl_logs
    │       ├── steward.py        DataSteward 用的查询
    │       ├── schema.py         运行时 schema 兜底（迁移用）
    │       └── utils.py          URL/email/homepage 规范化
    └── data_steward/         数据治理（清洗、查重、补抓任务下发）
        ├── agent.py
        └── db/
            ├── pipeline.py
            ├── repository.py
            └── selector.py
```

## 2. 入口

```
uv run yanclaw crawl --universities "北京航空航天大学" [--resume]
uv run yanclaw llm-check
uv run yanclaw cookie import <url> <file.json>
uv run yanclaw skills list / history / diff / rollback
uv run yanclaw steward run [--apply]
```

CLI 实现在 `runtime/crawler_cli.py`（`agents/crawler/cli.py` 只是兼容 shim）。

## 3. 数据存储模型

每所大学一个独立 SQLite，路径由 `_university_db_path` 计算：`data/universities/<eTLD+1>.db`（例如 `buaa.edu.cn.db`）。

| 表 | 用途 |
|---|---|
| `university_meta` | 该 DB 对应的大学元信息（每库一行） |
| `org_units` | 学院 / 院系 / 研究所；`(name unique, url unique)` |
| `professors` | 教师事实表，`(name, org_unit_name)` 不强制唯一但 upsert 路径用名字+学院对齐 |
| `academicians` | 院士专表，`(name, org_unit_id)` 唯一 |
| `professor_affiliations` | 教师↔学院 N:N，`(professor_id, org_unit_id)` 唯一 |
| `crawl_logs` | 抓取行为日志（每次 fetch 一行） |
| `crawl_tasks` | 流水线提取任务，`(source_url, org_unit_name, page_hash)` 唯一，用于断点续 |
| `crawl_extraction_failures` | LLM 提取失败/重试样本（不阻塞主流程） |
| `steward_runs` / `data_quality_audits` | DataSteward 运行记录 |

所有 ORM 在 `src/agents/crawler/models.py`，所有 CRUD 走 `src/agents/crawler/db/*.py`。

## 4. CrawlerAgent 状态机

定义在 `agent.py`（`CrawlerAgent` 类，`CrawlerState` 枚举）：

```
DISCOVER_ORG_UNIT_PAGES → EXTRACT_ORG_UNITS → FIND_FACULTY_PAGES → EXTRACT_PROFESSORS → DONE
```

`run()` 主循环里维持一个有限重试（`backtrack_count <= max_backtracks`），每个阶段失败可整体回退一次。

### 关键分支：`_is_interactive`

`HumanFetcherBridge` 暴露 `set_context`，因此 `CrawlerAgent._is_interactive == True` → 走 **streaming 模式**：
`_find_and_extract_streaming`（每个学院"找师资页 → 立刻抽老师 → 立刻补 detail"）。
非交互（httpx 等）走 batch 模式：先全部 `_find_faculty_pages`，再 `_extract_professors`。

当前默认 fetcher 是 `HumanFetcherBridge`（见 `dispatcher._default_fetcher_factory`）。**也就是线上跑的永远是 streaming 模式**，调试时要意识到这一点。

### EXTRACT_PROFESSORS 流水线

`_extract_professors(faculty_links)` 内部有两套路径：

1. **同步**（`pipeline_enabled=False`）：fetch → LLM 抽 → 同步存库 → 调用 detail 富化。
2. **异步流水线**（默认开启）：`_enqueue_extraction_task` 把任务塞进 `crawl_tasks` 表 + `asyncio.Queue`，由 `_pipeline_llm_worker` 调 LLM、`_pipeline_db_worker` 写库。`task_recovery_enabled=True` 启动时恢复 `pending/retry` 任务。

调度器为每个 faculty 列表页执行：

```python
fetched = await self._fetch_url(...)
await self._enqueue_extraction_task(...)         # 列表页 → LLM 异步队列
_schedule_related_pages(current, fetched, ...)    # 排入相关 followup / pagination
await self._enrich_profiles_with_detail_backend(...)  # **同步**爬每个老师 detail
```

`_enrich_profiles_with_detail_backend` 当前实现等价于 `agent_detail.enrich_profiles_with_human`（仅交互模式生效）。它在当前页面循环之前**同步**完成，所以 detail 抓取慢时整体节奏受限。

## 5. URL 启发式（`url_heuristics.py`）

这个文件 ~1100 行，是大部分行为的根源。常见职责：

| 函数 / 常量 | 用途 |
|---|---|
| `ORG_UNIT_PAGE_KEYWORDS` | 院系列表页关键词（jgsz/yxsz/zzjg/学院/院系…） |
| `FACULTY_KEYWORDS` | 师资页关键词（teacher/szdw/师资/教师…） |
| `_FACULTY_NOISE_*` | 噪声 URL（new/tzgg/recruit/dangjian/招生…） |
| `_is_non_faculty_noise_url` | 用于过滤新闻/通知/招聘等非师资页 |
| `_assess_faculty_candidate` / `_rank_faculty_page_candidates` | 师资候选评分+排序，分类成 `full_list / category_list / elite_list / noise_or_login` |
| `_select_balanced_faculty_candidates` | 在多类候选间挑出 4 个 |
| `_looks_like_org_unit_listing_url` / `_is_college_subdomain` | 院系页判断 |
| `_COMMON_FACULTY_PATHS` / `_INTERMEDIATE_ORG_PATHS` | 没有候选时的 path 探测列表 |
| `_FACULTY_LOGIN_HARD_REJECT_HINTS` | 登录/账号页硬拒绝 |

`agent_detail.py` 有自己的两套正向白名单：

- `_DETAIL_URL_HINTS`：`/info/`、`/teacher/`、`/show`、`/detail`、`/profile`、`teachershow`、`teacher_show` 等。决定哪些链接被当成"老师 detail 页"。
- `_CLEAR_PROFILE_DETAIL_HINTS`：更严格的版本，用于反向解锁被 `_is_faculty_directory_or_category_link` 误伤的 `/info/<id>/<id>.htm`。
- `_FACULTY_CATEGORY_STEMS`：`fjs/qzjs/szdw/...` 等，用于"这是分类列表页，不是 detail"。

### 命中规则（`extract_detail_profile_links`）

候选必须满足：同域 → 不是 faculty platform → 不是分页 → 不是退休 URL → 不是噪声 → 当前页若是噪声则不能 fanout → **`related_by_path` 或 `related_by_hint` 至少一个成立** → 经 `_score(...) >= 3` 排序。

> 重要：如果某高校的 detail URL 模板既不在 `_DETAIL_URL_HINTS` 里，也跟当前列表 path 没有公共前缀，**就会被静默丢弃**。日志里的体现是 `Detail links filtered ... kept=0`，但**没有**对应的 `dropped_*` 计数（因为这条分支没记数）。

## 6. Detail 富化（人工模式）

`agent_detail.enrich_profiles_with_human`：

1. `org_unit_key` = `id:{org_unit_id}` / `label:{label}` / `url:...`，按学院分桶记 `_detail_processed_by_org_unit`。
2. 每学院 `detail_profile_hard_cap_per_org_unit`（默认 200）。
3. 候选去重用两个集合：`_detail_visited_urls`（仅本 agent 内）和 `self.visited_urls`（所有已 fetch 过的 URL）。
4. `process_detail_urls_with_human` 顺序 fetch + `_extract_professors_from_page(detail_mode=True)`。

> 注意：每个 list 页迭代都会调用 enrich，**`_detail_visited_urls` 跨页累积**——分页子页若复用同一组 nav 链接，会被这里的去重直接屏蔽。

## 7. 数据写入（`tools.py`）

LLM 通过两个 tool 与代码交互：

- `save_professors(org_unit_name, professors[], org_unit_url?, source_url?)` —— 走 `sanitizer.sanitize_professor_payload`，根据 `is_academician` 分流到 `professors` 或 `academicians`，自动跑 `match_academician_for_professor` 做去重；最后 `ensure_professor_affiliation` 建关联（关联表的 `unique(professor_id, org_unit_id)` 是真正的唯一约束）。
- `extract_links(links[], base_url, keywords?)` —— 同域过滤 + 关键字过滤；关键字若把所有候选都干掉则保留同域全集。

> upsert 之后 DB 端 `professors` 表没有 `unique(name, org_unit)` 约束，只在 `professor_affiliations` 上有唯一。同名教师不同学院会被识别为不同记录。

## 8. 抓取层（fetchers）

- **`HumanFetcherBridge`**：默认 fetcher。aiohttp + Tampermonkey 用户脚本（`userscripts/`）。`fetch(url)` 把 `FetchJob` 放进 `JobQueue`，等用户脚本上报 HTML，再用 `Fetcher._html_to_text` + `extract_links_with_signals` 转换。
- **`Fetcher` (httpx_fetcher.py)**：当前实现实际上**没有 httpx 网络代码**了，只剩 `filter_same_domain`、`html_to_text`、`extract_links` 等纯函数 helper、加上 `FetchResult` dataclass 和 WAF 检测。
- **`extract_links_with_signals`**：返回 `(urls, LinkSignal[])`，每个信号附带 anchor text、最近的标题、父级 nav class——`_assess_structural_faculty_candidates` 会用这些上下文打分。

## 9. 配置（`CrawlerSettings`）

环境变量前缀 `YANCLAW_`，加载顺序 `.env` → 环境变量。常用项：

| 键 | 默认 | 说明 |
|---|---|---|
| `OPENAI_BASE_URL` / `OPENAI_API_KEY` / `OPENAI_MODEL` | `https://api.openai.com/v1` / 空 / `gpt-4o-mini` | 必须设置 key |
| `LLM_TEMPERATURE` / `LLM_TOP_P` / `LLM_SEED` | 0.0 / 1.0 / None | 决定性输出 |
| `MAX_CONCURRENCY` | 3 | 同时跑的大学数 |
| `REQUEST_INTERVAL_SECONDS` | 2.0 | per-domain 抓取间隔 |
| `MODEL_MAX_TOKENS` | 128000 | 上下文预算上限 |
| `DETAIL_ENRICH_ENABLED` | True | 关掉就只抓师资列表 |
| `DETAIL_PROFILE_HARD_CAP_PER_ORG_UNIT` | 200 | 每学院最多 detail 数 |
| `PIPELINE_ENABLED` | True | 关闭则改用同步路径 |
| `PIPELINE_LLM_WORKERS` / `PIPELINE_DB_WORKERS` | 1 / 1 | 并发数（注意 SQLite 有写锁） |
| `INVALID_JSON_MAX_RETRY` | 1 | LLM 输出非法 JSON 的重试次数 |
| `TASK_RECOVERY_ENABLED` | True | 启动时恢复 pending/retry crawl_tasks |
| `HUMAN_SERVER_HOST` / `_PORT` / `_JOB_TIMEOUT_SECONDS` | 127.0.0.1 / 21520 / 60 | 人工模式参数 |
| `WEBSITES_PATH` | `assets/entrances.yaml` | 大学入口清单；默认解析规范 YAML，`YANCLAW_WEBSITES_PATH` 可指向旧 CSV manifest |
| `UNIVERSITY_DB_DIR` | `data/universities` | DB 目录 |

## 10. Skills 体系

- 文件源：`src/agents/crawler/skills/*.md` + `src/runtime/skills/*.md`，frontmatter 携带 `name/description/version/created_at/updated_at`。
- `SkillManager`（`runtime/skills.py`）：当前 `.md` = 最新版；DB `skill_versions` 表存全部历史；修改前必须先把旧版写库。
- `_select_skills`（agent 里）：按状态决定加载 `extract-links / save-professors / crawler-loop-detection`，找不到就装载全部。
- 反思阶段（REFLECT）当前未启用，但 ORM 与 CLI 都已就绪。

## 11. 反震荡 / 去重

代码里散落多层（按生效次序）：

1. URL 启发式 → `_is_non_faculty_noise_url` / `_looks_like_retired_url` / `_is_faculty_platform`，过滤明显无关。
2. 同域 → `Fetcher.filter_same_domain` 用 `_site_root` (eTLD+1，特例处理 `.edu.cn`)。
3. 内存去重 → `self.visited_urls`（`_fetch_url` 入口判定）。
4. 跨运行去重 → `is_url_crawled` 查 `crawl_logs.status='success'`；可被 `_skip_cross_run_dedup` 临时旁路。
5. 流水线任务去重 → `crawl_tasks(source_url, org_unit_name, page_hash)` 唯一约束 + `_mark_scheduled` / `_mark_processing` 内存集合。
6. Detail 子流程 → `_detail_visited_urls`（**整个 agent 生命周期累积**，跨学院同 host 也会互相影响）。
7. 数据库去重 → `professor_affiliations` 唯一 + `match_academician_for_professor` 名字/邮箱/主页匹配。

## 12. 调试入口

- 日志：`logs/crawl_<YYYYMMDD_HHMMSS>.log`，文件级 DEBUG，含每次 fetch、LLM 请求/响应、tool 调用 args/result 摘要、流水线统计。
- 数据库：`data/universities/<host>.db`，可直接 `sqlite3` 看 `professors` / `crawl_tasks` / `crawl_extraction_failures`。
- 流水线统计：每次 EXTRACT_PROFESSORS 结束在日志里打印一行 `Extraction pipeline stats ...`，包括 `processed/retries/failed/avg_task_ms/llm_calls_total/skipped_by_gate/followups/pagination/duplicate_skipped`。
- 关键 log 模式：
  - `Detail links filtered current=... kept=N dropped_noise=A dropped_directory=B` —— detail 候选筛选结果
  - `Followup links filtered ... kept=N` —— 同列表的相关页
  - `Faculty assessment details ... preview=...` —— faculty 候选评分
  - `Skip professor LLM ... reason=...` —— `_should_skip_professor_llm` 规避无意义请求
  - `Invalid JSON in tool arguments` —— LLM 输出非法 JSON，触发严格重试

## 13. 已知雷点 / 调试经验（基于最近一次 BUAA 跑批）

1. **[已修]** `teachershouw.jsp?urltype=news.NewsContentUrl&...`（BUAA siteweaver 的师资 detail）原本会被 `_iter_url_noise_tokens` 切到 query 里的 `news` token 当 noise 干掉，整个 41 人的软件学院 detail 全空。修复：`_iter_url_noise_tokens` 改成只 tokenize path（`url_heuristics.py`）。`_FACULTY_NOISE_URL_HINTS` 仍以 path 子串的方式覆盖正经的 `/news/` 类页面。日志里的诊断信号是 `Detail links filtered ... kept=0` 时同步出现 `dropped_noise=N` 远超预期。

2. **[已修]** 同一老师在多个分类列表里（博导/硕导/杰出人才/教师名录）会触发多次 detail 抓取——BUAA CMS 给同一人不同 channel 的 `/info/<channelId>/<articleId>.htm`，URL 不同 → `visited_urls` 不能去重。修复：`enrich_profiles_with_human` 在加 detail 候选前用 `fetched.link_signals.anchor_text` 做名字级匹配，已富化的人（DB 中 `research_areas` 或 `bio` 非空）直接跳过；处理完一批 detail 后清掉 `_enriched_names_by_org_unit[key]` 缓存让下一个列表页拿到新数据。统计计数 `detail_links_dropped_already_enriched`。注意 anchor 匹配走的是子串包含 + 头衔噪声去除（教授 / 副教授 / 讲师 / professor / associate 等），命中即跳。

3. **[半修]** 分页子页 `fjs/1.htm`–`fjs/5.htm` 的 detail 富化会被首页处理过的 `_detail_visited_urls` 屏蔽，导致 60 副教授只有 12 个抓到详情。这次改动**没有**真正修这个底层问题（需要要么 LLM 反查、要么 BUAA 特定 URL 模式拼装），但加了诊断 WARNING：当 `candidates>0` 但 `pending=0` 时打印整段 `org_unit / page / sample / skipped_by_name` 信息，并增加 `detail_pending_empty_with_candidates` 计数。下次跑批可凭此快速定位是不是同一类问题。

4. **流水线写库 worker 数量** —— SQLite 默认 1 个写者，多于 1 会概率性 `database is locked`。`PIPELINE_DB_WORKERS=1` 是默认值，调高前要确认 WAL/timeout 配置。

5. **`_extract_professors_from_page(detail_mode=True)` 会触发严格重试** —— 同一 URL 可能在日志里出现两次 `LLM extraction payload task_id=0`，`payload_bytes` 略不同。这是 `invalid_json_max_retry` 的正常行为，不是循环。

6. **`crawl_logs` 不区分大学** —— 表里只存 URL 和 status；用同一 host 去定位时要小心多跑批的混淆。`crawl_tasks` 有 `university` 字段，更适合调试。

## 14. 新增内容时

- 新启发式：放进 `url_heuristics.py`；新加 `_DETAIL_URL_HINTS`/`_CLEAR_PROFILE_DETAIL_HINTS` 时记得在 `tests/unit/crawler/test_url_validation.py` 加用例。
- 新 fetcher：放在 `src/agents/crawler/fetchers/`，至少要实现 `__aenter__` / `__aexit__` / `fetch(url) -> FetchResult` / `filter_same_domain` 静态方法；`set_status_provider` 可选；如果是交互式必须暴露 `set_context`（`_is_interactive` 据此判定）。
- 新 tool：`tools.py` 加定义 + handler，并在 `_ask_llm` 的 `allowed_tools` 集合里放行；同时更新 `agents/crawler/skills/save-professors.md` 等告诉 LLM 怎么用。
- 新数据字段：`models.py` 新列 → 在 `db/schema.py:ensure_runtime_schema` 里补"老库不存在则 ALTER TABLE"；同步改 `sanitizer.py` 与 `db/professors.py` 的 upsert。
- 新大学：优先编辑 `assets/entrances.yaml`，写正式高校名、正式学院名和入口 URL；`assets/收集.txt` 只保留为人工原始采集资料，不再由 crawler 解析。`YANCLAW_WEBSITES_PATH` 仍可指向旧 CSV manifest。

## 15. 测试

```
uv run pytest tests/unit/crawler/...
```

关键测试：

- `tests/unit/crawler/test_url_validation.py` —— 启发式正例/反例
- `tests/unit/crawler/test_agent.py` —— 状态机 + 流水线最小化
- `tests/unit/crawler/test_db.py` —— upsert/affiliation/院士匹配
- `tests/unit/crawler/test_human_bridge.py` —— job 队列与 decision

每次改 `url_heuristics` 或 `agent_detail` 之前，先跑这套，避免回归。
