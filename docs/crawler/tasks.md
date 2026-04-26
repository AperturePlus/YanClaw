# Yanclaw Crawler Agent — 任务分解

## Task 1: 初始化项目结构和依赖

**目标**: 建立双层包结构（runtime + agents/crawler），配置所有依赖。

**实现**:
- 填写 `pyproject.toml`：
  - 运行依赖：httpx, html2text, openai, sqlalchemy, aiosqlite, click, pydantic, pydantic-settings, python-dotenv, tiktoken
  - 开发依赖：pytest, pytest-asyncio
- 创建 `src/runtime/__init__.py`
- 创建 `src/agents/crawler/__init__.py`
- 创建 `src/agents/crawler/config.py`：`CrawlerSettings(BaseSettings)` 从环境变量 / `.env` 加载配置
- 创建 `.env.example` 列出所有配置项

**测试**: `uv sync` 成功安装所有依赖；`python -c "from agents.crawler.config import CrawlerSettings"` 正常执行。

**Demo**: 项目可安装，配置可加载。

---

## Task 2: 实现 runtime 日志模块

**目标**: 结构化日志系统，控制台 INFO 摘要 + 文件 DEBUG 详细。

**实现**:
- 创建 `src/runtime/logger.py`
- `setup_logging(log_dir: str)` — 配置 root logger `yanclaw`，添加 StreamHandler (INFO) + FileHandler (DEBUG)
- `get_logger(name: str) -> Logger` — 返回 child logger（如 `yanclaw.crawler.北京大学`）
- 控制台格式：`[HH:MM:SS] [name] 消息`
- 文件格式：`[ISO时间] [级别] [name] 消息`
- 日志文件路径：`logs/crawl_{YYYYMMDD_HHMMSS}.log`

**测试**:
- 验证 `setup_logging()` 后日志同时输出到控制台和文件
- 验证文件包含 DEBUG 级别日志而控制台只有 INFO+
- 验证日志格式正确

**Demo**: 运行测试脚本，控制台显示简洁日志，`logs/` 目录下生成详细日志文件。

---

## Task 3: 实现 runtime 数据库基础设施

**目标**: async engine/session 工厂 + SkillVersion 共享模型。

**实现**:
- 创建 `src/runtime/database.py`
- `DatabaseManager` 类：
  - 持有 async engine（`aiosqlite`）
  - `session()` — async context manager，返回 `AsyncSession`
  - `init_db()` — 创建所有表
- `SkillVersion` SQLAlchemy 模型：id, skill_name, version, content, change_summary, agent_name, created_at

**测试**:
- 测试 `init_db()` 创建表
- 测试 SkillVersion CRUD（插入、查询、按 skill_name 过滤）

**Demo**: SQLite 文件生成，SkillVersion 可读写。

---

## Task 4: 实现 runtime SkillManager

**目标**: Skill 加载/保存/版本管理/回滚/diff。

**实现**:
- 创建 `src/runtime/skills.py`：`SkillManager` 类
  - 构造参数：`skills_dir: Path`, `db: DatabaseManager`, `agent_name: str`
  - `list_skills() -> list[SkillMeta]` — 解析所有 `.md` 的 frontmatter，返回 name + description
  - `load_skill(name: str) -> str` — 读取 `.md` 全文
  - `load_skills(names: list[str]) -> dict[str, str]` — 批量加载
  - `update_skill(name, new_content, change_summary)` — **先**将当前版本存入 DB → 写入新 `.md` → 递增 version
  - `create_skill(name, content, description, change_summary)` — 创建新 `.md` 文件
  - `rollback_skill(name, target_version)` — 存当前版本到 DB → 从 DB 取目标版本 → 覆盖 `.md`
  - `diff_skill(name, v1: int, v2: int) -> str` — `difflib.unified_diff` 对比两个版本
  - `get_history(name) -> list[VersionInfo]` — 返回版本列表
- 创建 `src/runtime/skills/create-skills.md`（元 skill，教 LLM 如何创建/修改 skills，强制要求修改前存储老版本）
- Frontmatter 解析：简单解析 `---` 块中的 YAML

**测试**:
- 测试 list_skills 解析 frontmatter
- 测试 update_skill 时旧版本自动存入 DB
- 测试 rollback 后 `.md` 文件内容恢复正确
- 测试 diff 输出格式
- 测试完整流程：create → update 两次 → get_history → diff v1 vs v2 → rollback to v1

**Demo**: 创建 skill → 修改两次 → 查看历史 → diff → 回滚 → 验证文件内容。

---

## Task 5: 实现 runtime ContextManager

**目标**: Token 预算管理 + 长页面分块。

**实现**:
- 创建 `src/runtime/context.py`：`ContextManager` 类
  - `count_tokens(text: str) -> int` — 用 tiktoken 计算 token 数
  - `build_messages(system_prompt, tool_defs, skills_text, user_content, max_tokens) -> list[list[dict]]`：
    - 按优先级分配预算：system prompt > tool defs > skills > content
    - 内容未超预算 → 返回单组 messages
    - 内容超预算 → 分块，返回多组 messages
  - `chunk_text(text: str, max_tokens: int) -> list[str]` — 按段落边界切分
  - `truncate_list(items: list[str], max_tokens: int) -> list[str]` — 截断列表（用于 visited_urls）

**测试**:
- 测试 token 计数准确性
- 测试短文本直接通过（返回单组 messages）
- 测试长文本被正确分块（返回多组 messages）
- 测试 truncate_list 截断逻辑

**Demo**: 给定超长文本，验证分块结果合理。

---

## Task 6: 实现 runtime LLMClient

**目标**: 封装 OpenAI 兼容 API，实现 tool calling 循环。

**实现**:
- 创建 `src/runtime/llm.py`：`LLMClient` 类
  - 构造参数：base_url, api_key, model
  - `async chat(messages, tools, tool_handlers) -> LLMResult`：
    1. 发送请求到 LLM
    2. 若响应包含 `tool_calls` → 执行对应的 `tool_handlers[tool_name](**args)` → 将结果追加到 messages → 再次调用 LLM
    3. 循环直到 LLM 返回普通文本或达到最大轮次 (`max_rounds`)
    4. 返回 `LLMResult(content, tool_call_log: list[ToolCallRecord])`
  - `tool_handlers: dict[str, Callable]` — tool name → async handler function
  - 每次 LLM 调用和 tool 执行都记录日志（logger）

**测试**:
- Mock openai client，测试普通对话（无 tool call）
- 测试单轮 tool call（LLM 返回 tool_call → 执行 → LLM 返回文本）
- 测试多轮 tool call 循环
- 测试最大轮次限制（超过 max_rounds 时停止）

**Demo**: 用 mock 数据运行 LLM 对话，日志展示 tool calling 循环过程。


---

## Task 7: 实现 crawler 业务数据库层

**目标**: Crawler 专属 ORM 模型 + 业务 CRUD。

**实现**:
- 创建 `src/agents/crawler/models.py`：University, College, Professor, CrawlLog 四个 SQLAlchemy 模型
- 创建 `src/agents/crawler/db.py`：
  - `upsert_professor(session, data)` — INSERT ON CONFLICT UPDATE（基于 unique(name, college_id)）
  - `log_crawl(session, university_id, url, status, message)` — 记录抓取日志
  - `is_url_crawled(session, url) -> bool` — 查询 URL 是否已成功抓取（跨运行去重）
  - `get_university_status(session, name) -> CrawlStatus` — 查询大学爬取状态
  - `set_university_status(session, name, status)` — 更新大学爬取状态
  - `load_universities_from_csv(session, path)` — 从 `assets/websites.md` 导入大学列表

**测试**:
- 测试 upsert_professor 插入和更新（同名同学院 → 更新而非重复插入）
- 测试 is_url_crawled 去重逻辑
- 测试 get/set_university_status 状态管理
- 测试 load_universities_from_csv 导入

**Demo**: 数据可插入/查询/更新，SQLite 文件中记录正确。

---

## Task 8: 实现 crawler Fetcher

**目标**: 异步 HTTP 抓取 + per-domain 限速 + 重试 + HTML → 文本。

**实现**:
- 创建 `src/agents/crawler/fetcher.py`：`Fetcher` 类
  - 持有 `httpx.AsyncClient`，实现 `async with` 生命周期管理
  - per-domain 限速：`dict[str, float]` 记录每个域名上次请求时间，请求前 `asyncio.sleep` 补足间隔
  - 指数退避重试：429/503/超时 → `delay * 2^attempt`，最多 `max_retries` 次
  - HTML → 文本：`html2text` 转换，保留链接 `[text](url)`
  - 提取页面中所有链接并绝对化处理
  - `filter_same_domain(links, base_url) -> list[str]` — 同域名/子域名过滤
  - 返回 `FetchResult(url, text, links, status_code)` 数据类

**测试**:
- 用 mock 测试限速逻辑（两次同域名请求间隔 ≥ 配置值）
- 用 mock 测试重试逻辑（模拟 429 → 成功）
- 测试 HTML → 文本转换和链接提取
- 测试同域名过滤（允许子域名，拒绝外部域名）

**Demo**: 测试全部通过。

---

## Task 9: 实现 crawler Tools + Skills

**目标**: 定义 crawler 的业务 tools 和预设 skills。

**实现**:
- 创建 `src/agents/crawler/tools.py`：
  - `save_professors` tool definition (JSON schema) + async handler（调用 `db.upsert_professor`）
  - `extract_links` tool definition + async handler（返回筛选后的链接列表）
  - `get_crawler_tools(db, skill_manager) -> dict` — 返回 tool_name → handler 映射，包含：
    - 业务 tools：save_professors, extract_links
    - runtime skill tools：update_skill, create_skill（委托给 SkillManager）
- 创建 skill 文件：
  - `src/agents/crawler/skills/save-professors.md` — 包含 tool schema、使用指南、注意事项
  - `src/agents/crawler/skills/extract-links.md` — 包含 tool schema、链接筛选策略

**测试**:
- 测试 save_professors handler 调用后数据写入 DB
- 测试 extract_links handler 返回正确格式
- 验证 skill 文件 frontmatter 格式合规

**Demo**: 调用 tool handler，数据正确写入 DB。

---

## Task 10: 实现 CrawlerAgent 状态机

**目标**: 单所大学的完整爬取流程 + 路径震荡防护 + 执行后反思。

**实现**:
- 创建 `src/agents/crawler/agent.py`：`CrawlerAgent` 类
  - 接收 runtime 原语（LLMClient, SkillManager, ContextManager, DatabaseManager, Logger）+ Fetcher
  - 状态机：`FIND_COLLEGES → FIND_FACULTY_PAGES → EXTRACT_PROFESSORS → REFLECT → DONE`
  - `visited_urls: set[str]` + `db.is_url_crawled()` 双重去重
  - 路径震荡防护：
    - `max_depth = 4`：从首页算起的最大点击深度
    - `max_backtracks = 3`：允许的最大回退次数
    - `backtrack_count` 计数器
    - 同域名约束：只跟踪同域名/子域名链接
  - 每个阶段开始前：调用 LLM 从 skill_metas 选择要加载的 skills
  - 每步：用 ContextManager 构建 messages → LLMClient.chat() → 处理结果
  - **REFLECT 阶段**：LLM 回顾执行日志摘要，决定是否 update_skill / create_skill
  - 每个 URL 抓取后写 CrawlLog
  - 完成后更新 `University.crawl_status`

**测试**:
- Mock fetcher + LLMClient，测试状态机完整流转（含 REFLECT）
- 测试 visited_urls 去重（同一 URL 不会调用 fetcher 两次）
- 测试 max_depth 限制（超过深度的链接被跳过）
- 测试 max_backtracks 限制（超过回退次数后标记 failed）
- 测试 REFLECT 阶段触发 skill 更新

**Demo**: 用 mock 运行完整 Agent 流程，日志清晰展示状态机流转、skill 选择、去重决策、反思过程。

---

## Task 11: 实现 CrawlDispatcher + CLI

**目标**: 并行调度多个 Agent + 命令行入口。

**实现**:
- 创建 `src/agents/crawler/dispatcher.py`：`CrawlDispatcher` 类
  - 解析 `assets/websites.md` 获取大学列表
  - `asyncio.Semaphore(max_concurrency)` 控制并发
  - 共享 Fetcher 实例（per-domain 限速跨 Agent 生效）
  - 断点续爬：跳过 `crawl_status=completed` 的大学
  - 支持 `--universities` 过滤
  - 运行结束输出汇总报告（成功/失败/跳过）
- 创建 `src/agents/crawler/cli.py`：Click 命令组
  - `yanclaw crawl` — 启动爬取（`--universities`, `--concurrency`, `--log-dir`）
  - `yanclaw skills list` — 列出所有 skills
  - `yanclaw skills history <name>` — 查看 skill 版本历史
  - `yanclaw skills diff <name> <v1> <v2>` — 查看两个版本的 diff
  - `yanclaw skills rollback <name> <version>` — 回滚 skill 到指定版本
- 在 `pyproject.toml` 中注册 CLI entry point

**测试**:
- Mock Agent，测试并发控制（Semaphore 限制生效）
- 测试大学过滤逻辑
- 测试断点续爬（已完成的大学被跳过）
- 测试 CLI 参数解析（`--help` 输出正确）

**Demo**: `yanclaw crawl --help` 正常输出；用 mock Agent 运行 dispatcher，控制台显示调度过程和汇总。

---

## Task 12: 集成测试

**目标**: 端到端验证完整流程。

**实现**:
- 创建 `tests/agents/crawler/test_integration.py`
- Mock HTTP 响应（准备 2-3 个模拟的大学页面 HTML）和 LLM 响应
- 测试完整流程：CrawlDispatcher → CrawlerAgent → Fetcher → LLMClient → DB
- 验证项：
  - 数据库中有正确的 University/College/Professor 记录
  - CrawlLog 记录完整且状态正确
  - 重复运行时跳过已完成的 URL（去重生效）
  - REFLECT 阶段 skill 版本变更写入 DB
  - 日志文件生成且内容可追溯
- 测试目录结构：
  ```
  tests/
  ├── unit/
  │   ├── runtime/          # runtime 各模块单元测试
  │   └── crawler/          # crawler 各模块单元测试
  └── integration/
      └── crawler/          # crawler 集成测试
  ```

**Demo**: `uv run pytest tests/ -v` 全部通过。
