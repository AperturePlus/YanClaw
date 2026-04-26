# Yanclaw Crawler Agent — 设计文档

## 1. 架构总览

系统分为两层：**runtime（共享运行时）** 和 **agents（业务智能体）**。

- `runtime` 提供所有 agent 共享的运行时原语：LLM 调用、tool calling 循环、skill 管理与版本控制、上下文窗口管理、数据库基础设施、结构化日志。
- `agents/crawler` 在 runtime 之上实现自主探索导师信息的业务逻辑：状态机、网页抓取、业务 tools、并行调度。

runtime **不**提供 workflow、状态机或 BaseAgent 抽象类，避免成为上帝对象。各 agent 用 runtime 原语自行编排业务流程。

```
┌──────────────────────────────────────────────────────┐
│                   CLI (click)                         │
│             yanclaw crawl <options>                   │
│             yanclaw skills <subcommand>               │
└────────────────────────┬─────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────┐
│              CrawlDispatcher                          │
│  - 解析 assets/websites.md                            │
│  - asyncio.Semaphore 控制并发                          │
│  - 共享 Fetcher 实例                                   │
│  - 断点续爬 + 汇总报告                                 │
└──────────┬──────────┬───────────┬────────────────────┘
           │          │           │
           ▼          ▼           ▼
     ┌──────────┐ ┌──────────┐ ┌──────────┐
     │ Agent(A) │ │ Agent(B) │ │ Agent(C) │  (每所大学一个)
     └────┬─────┘ └────┬─────┘ └────┬─────┘
          │            │            │
          ▼            ▼            ▼
    ┌───────────────────────────────────────────┐
    │         runtime 共享原语                    │
    │  ┌───────────┐ ┌──────────────┐           │
    │  │ LLMClient │ │ SkillManager │           │
    │  └───────────┘ └──────────────┘           │
    │  ┌────────────────┐ ┌─────────────────┐   │
    │  │ ContextManager │ │ DatabaseManager │   │
    │  └────────────────┘ └─────────────────┘   │
    │  ┌────────┐                               │
    │  │ Logger │                               │
    │  └────────┘                               │
    └───────────────────────────────────────────┘
          │
          ▼
    ┌───────────────────────────────────────────┐
    │         crawler 业务组件                    │
    │  ┌─────────┐ ┌───────┐ ┌──────────────┐  │
    │  │ Fetcher │ │ Tools │ │ 业务 DB/ORM  │  │
    │  │ (httpx) │ │       │ │              │  │
    │  └─────────┘ └───────┘ └──────────────┘  │
    └───────────────────────────────────────────┘
```

## 2. 项目结构

```
src/
├── runtime/                    # 共享运行时原语
│   ├── __init__.py
│   ├── llm.py                  # LLMClient: chat + tool calling 循环
│   ├── skills.py               # SkillManager: 加载/保存/版本/回滚/diff
│   ├── context.py              # ContextManager: token 预算 + 分块
│   ├── database.py             # DatabaseManager: async engine/session + SkillVersion 模型
│   ├── logger.py               # setup_logging + get_logger
│   └── skills/                 # 共享元 skills
│       └── create-skills.md    # 教 LLM 如何创建/修改 skills
├── agents/
│   ├── crawler/                # Crawler 智能体
│   │   ├── __init__.py
│   │   ├── config.py           # CrawlerSettings (Pydantic Settings)
│   │   ├── models.py           # ORM: University, College, Professor, CrawlLog
│   │   ├── db.py               # 业务 CRUD: upsert_professor, log_crawl, is_url_crawled
│   │   ├── fetcher.py          # httpx 异步抓取 + per-domain 限速 + 重试
│   │   ├── tools.py            # tool definitions + handlers
│   │   ├── agent.py            # CrawlerAgent 状态机
│   │   ├── dispatcher.py       # CrawlDispatcher 并行调度
│   │   ├── cli.py              # Click CLI 入口
│   │   └── skills/             # crawler 专属 skills
│   │       ├── save-professors.md
│   │       └── extract-links.md
│   └── retreiver/              # 未来的 Retreiver 智能体
```

## 3. 数据模型

### 3.1 Runtime 共享模型

```
SkillVersion                        # 技能历史版本（所有 agent 共享）
├── id: int (PK)
├── skill_name: str                 # 如 "save-professors"
├── version: int                    # 版本号
├── content: text                   # 该版本的完整 markdown 内容
├── change_summary: str             # LLM 写的变更说明
├── agent_name: str                 # 所属 agent，如 "crawler"
└── created_at: datetime
```

### 3.2 Crawler 业务模型

```
University
├── id: int (PK)
├── name: str (unique)
├── url: str
├── location: str
└── crawl_status: enum(pending, in_progress, completed, failed)

College
├── id: int (PK)
├── name: str
├── url: str | None
├── university_id: int (FK → University)
└── unique(name, university_id)

Professor
├── id: int (PK)
├── name: str
├── title: str | None               # 职称
├── research_areas: str | None      # 研究方向（JSON 数组序列化）
├── email: str | None
├── phone: str | None
├── homepage: str | None
├── bio: str | None                  # 履历
├── enrollment_pref: str | None      # 招生偏好
├── publications: str | None         # 学术成果
├── college_id: int (FK → College)
└── unique(name, college_id)

CrawlLog
├── id: int (PK)
├── university_id: int (FK)
├── url: str
├── status: enum(success, failed, skipped)
├── message: str | None
└── created_at: datetime
```


## 4. Skills 体系

### 4.1 概述

Skills 是给 LLM Agent 看的指令集，以 markdown 文件形式存在。每个 agent 有独立的 `skills/` 目录，runtime 层有共享的元 skill。LLM 可以在执行后总结经验，创建或修改 skills，实现自我进化。

### 4.2 Skill 文件规范

每个 `.md` 文件遵循统一格式：

```markdown
---
name: save-professors
description: 从师资页面提取教师信息并批量保存到数据库
version: 3
created_at: 2026-04-26T17:00:00
updated_at: 2026-04-27T10:30:00
---

## 目标
[这个 skill 要完成什么]

## 工具定义
[对应的 tool schema，JSON 格式]

## 执行指南
[LLM 应该如何使用这个 skill 的详细指令]

## 注意事项
[边界情况、常见错误、经验总结]
```

### 4.3 版本管理

- 当前 `.md` 文件 = **最新生效版本**
- 数据库 `SkillVersion` 表存储**所有历史版本**（全量内容）
- **修改前必须先存储老版本到 DB**，系统强制执行此约束
- 支持回滚到任意历史版本（从 DB 取出 → 覆盖 `.md` 文件）
- 支持任意两个版本间的 diff 对比（`difflib` unified diff）

### 4.4 Skill 加载策略

LLM 不是每次加载全部 skills，而是**自行选择**：

1. 每个阶段开始前，系统将所有 skill 的 `name + description` 列表发送给 LLM
2. LLM 根据当前任务选择需要加载的 skills
3. 系统加载选中的 skill 全文，注入到 LLM 上下文中
4. prompt 中附带 skill 选择的指导（如"你正在执行 FIND_COLLEGES 阶段，请选择相关的 skills"）

### 4.5 自我进化机制

Agent 状态机包含 **REFLECT** 阶段（在 EXTRACT_PROFESSORS 之后、DONE 之前）：

1. 系统将本次执行日志摘要发送给 LLM
2. LLM 判断：本次遇到了什么问题？如何解决？执行与 skills 指示有哪些出入？
3. LLM 决定是否需要 `update_skill`（修改现有 skill）或 `create_skill`（创建新 skill）
4. 若修改，系统自动将旧版本存入 DB，再写入新内容

### 4.6 Skill 清单

| Skill | 位置 | 说明 |
|-------|------|------|
| `create-skills.md` | `runtime/skills/` | 元 skill，教 LLM 如何创建/修改 skills，所有 agent 共享 |
| `save-professors.md` | `agents/crawler/skills/` | 教师信息提取与保存的 tool 使用指南 |
| `extract-links.md` | `agents/crawler/skills/` | 链接筛选与导航决策的策略指南 |

## 5. 路径震荡防护

Agent 自主探索时可能陷入循环（A→B→A→B...）或在无关页面间来回跳转。以下五层机制防止路径震荡：

### 5.1 已访问 URL 集合（双重去重）

- **运行内**: Agent 维护 `visited_urls: set[str]`，抓取前检查
- **跨运行**: 抓取前查询 `CrawlLog` 表，若该 URL 已有 `status=success` 记录则跳过
- 同一 URL 绝不抓取两次

### 5.2 最大探索深度 (max_depth=4)

从大学首页算起，限制最大点击深度。典型路径：

```
深度 0: 大学首页
深度 1: 院系列表页 / 导航页
深度 2: 某学院首页
深度 3: 师资列表页
深度 4: 教师个人主页（最大深度）
```

超过 max_depth 的链接直接跳过，不再深入。

### 5.3 有限回退 (max_backtracks=3)

允许 Agent 在找不到目标时回退到上一阶段尝试其他路径，但设置上限：

- 每个 Agent 维护 `backtrack_count` 计数器
- 每次回退 +1，超过 `max_backtracks` 则标记当前分支为 failed，继续下一个学院
- 防止 Agent 在两个阶段间无限来回

### 5.4 同域名约束

Agent 只跟踪与大学官网**同域名或已知子域名**的链接：

- `www.pku.edu.cn` → 允许 `cs.pku.edu.cn`、`math.pku.edu.cn`
- 不允许跳转到 `baidu.com`、`google.com` 等外部站点
- 由 `Fetcher.filter_same_domain()` 实现

### 5.5 LLM Prompt 约束

在发送给 LLM 的 prompt 中明确告知：

- 已访问过的 URL 列表（或最近 N 条）
- "不要推荐已经访问过的链接"
- "只推荐与当前任务相关的链接"

## 6. 上下文溢出处理

### 6.1 Token 预算分配

```
总预算 = model_max_tokens - reserved_for_response(2000)

分配优先级（从高到低）：
┌─────────────────────┬──────────┬───────────────────────┐
│ 组件                 │ 优先级    │ 策略                   │
├─────────────────────┼──────────┼───────────────────────┤
│ System prompt       │ 最高      │ 固定，不可压缩          │
│ Tool definitions    │ 高        │ 固定，不可压缩          │
│ 已加载的 Skills     │ 中高      │ LLM 按需选择，减少数量  │
│ 页面内容            │ 中        │ 超预算时分块处理        │
│ 已访问 URL 列表     │ 低        │ 超出时截断为最近 N 条   │
└─────────────────────┴──────────┴───────────────────────┘
```

### 6.2 长页面分块处理

当页面内容的 token 数超过剩余预算时：

1. 按段落边界将页面切分为多个 chunks
2. 每个 chunk 独立发送给 LLM 处理
3. 合并各 chunk 的 `ParseResult`（教师列表合并、链接列表去重合并）

### 6.3 已访问 URL 截断

当 `visited_urls` 列表过长时，只保留最近 N 条（按访问时间排序），确保不超出 token 预算。LLM prompt 中注明"以下是最近访问的 URL，完整列表已在系统中记录"。


## 7. 日志设计

使用 Python `logging` 模块，控制台 + 文件双输出。

### 7.1 输出配置

| 输出 | 级别 | 格式 | 目标 |
|------|------|------|------|
| 控制台 | INFO | `[HH:MM:SS] [name] 消息` | 运行时摘要 |
| 文件 | DEBUG | `[ISO时间] [级别] [name] 消息` | 完整追溯 |

日志文件路径：`logs/crawl_{YYYYMMDD_HHMMSS}.log`

### 7.2 Logger 层级

```
yanclaw                          # root
├── yanclaw.runtime              # runtime 层日志
│   ├── yanclaw.runtime.llm      # LLM 调用
│   ├── yanclaw.runtime.skills   # skill 管理
│   └── yanclaw.runtime.db       # 数据库操作
└── yanclaw.crawler              # crawler 层日志
    ├── yanclaw.crawler.北京大学   # 每所大学一个 child logger
    ├── yanclaw.crawler.清华大学
    └── ...
```

### 7.3 必须记录的事件

- 页面抓取：URL、状态码、耗时
- LLM 调用：prompt 摘要、model、token 用量、耗时
- Tool 执行：tool name、参数摘要、结果摘要
- 数据库写入：插入/更新的记录数
- 回退决策：原因、当前 backtrack_count
- 跳过原因：已访问 / 超深度 / 非同域名 / 已完成

## 8. 去重机制

### 8.1 URL 去重（三层）

| 层级 | 机制 | 作用域 |
|------|------|--------|
| 运行内 | `visited_urls: set[str]` | 单次运行内，同一 URL 不重复抓取 |
| 跨运行 | 查询 `CrawlLog(url, status=success)` | 重启后跳过已成功抓取的 URL |
| 大学级 | 查询 `University.crawl_status=completed` | 重启后跳过已完成的大学 |

### 8.2 数据去重

- `Professor` 表 `unique(name, college_id)` 约束
- 写入时使用 upsert：存在则 UPDATE，不存在则 INSERT
- 保证同一学院下同名教师不会重复

## 9. CrawlerAgent 状态机

```
START
  │
  ▼
FIND_COLLEGES ◄─────────────────┐
  │                              │
  │ (发现学院列表)                │ 回退 (backtrack_count < max)
  ▼                              │
FIND_FACULTY_PAGES ◄────────┐   │
  │                          │   │
  │ (找到师资页面)            │ 回退
  ▼                          │   │
EXTRACT_PROFESSORS ──────────┘───┘
  │
  │ (所有学院处理完毕)
  ▼
REFLECT
  │
  │ (LLM 总结经验，可选更新 skills)
  ▼
DONE
```

每个阶段的 Agent 行为：

1. **FIND_COLLEGES**: 抓取大学首页 → LLM 分析 → 提取学院列表页链接
2. **FIND_FACULTY_PAGES**: 对每个学院，抓取学院页面 → LLM 找到"师资队伍"链接
3. **EXTRACT_PROFESSORS**: 抓取师资列表 → LLM 提取教师信息 + 个人主页链接 → 抓取详情 → tool calling 存库
4. **REFLECT**: LLM 回顾执行日志，总结经验，决定是否更新/创建 skills
5. **DONE**: 更新 `University.crawl_status = completed`

## 10. CrawlDispatcher 调度器

CrawlDispatcher 是 crawler 子系统内部的调度器（非 Yanclaw 顶层编排）。

- 从 `assets/websites.md` 解析大学列表
- `asyncio.Semaphore(max_concurrency)` 控制同时运行的 Agent 数量
- **共享 Fetcher 实例**：per-domain 限速跨 Agent 生效，避免多个 Agent 同时请求同一域名
- **断点续爬**：检查 `University.crawl_status`，跳过已 `completed` 的大学
- **过滤**：支持 `--universities` 参数指定大学子集
- **汇总报告**：运行结束输出成功/失败/跳过计数

## 11. 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 解析方式 | LLM 而非静态解析 | 各高校网站结构差异极大，维护几十套规则成本高且脆弱 |
| 数据库 | SQLite | 单机运行，零配置，通过 SQLAlchemy 抽象可切换 |
| HTTP 客户端 | httpx 而非 Playwright | 高校师资页面多为 SSR，httpx 更轻量高效 |
| 限速策略 | per-domain | 避免对单个网站造成压力，同时保持整体吞吐量 |
| LLM 封装 | 自行封装，不用 langchain | 保持轻量，避免不必要的抽象层 |
| runtime 职责 | 提供原语，不提供 workflow | 避免 runtime 成为上帝对象，各 agent 自行编排 |
| Skill 版本存储 | 数据库而非 git | 避免与项目 git 产生交叉覆盖 |

## 12. 错误处理策略

| 错误类型 | 处理流程 |
|----------|----------|
| 网络错误 | 指数退避重试 → 记录 CrawlLog(failed) → 跳过，继续下一个 |
| LLM 错误 | 重试 → 记录日志 → 跳过当前页面 |
| 解析错误 | 记录 LLM 原始响应 → 跳过 |
| 数据库错误 | 事务回滚 → 记录日志 |

原则：**单点失败不阻塞整体流程**。

## 13. 依赖

| 包 | 用途 |
|----|------|
| `httpx` | 异步 HTTP 客户端 |
| `html2text` | HTML → 纯文本（保留链接） |
| `openai` | LLM API 调用（OpenAI 兼容接口） |
| `sqlalchemy` | ORM + 数据库操作 |
| `aiosqlite` | SQLAlchemy 异步 SQLite 驱动 |
| `click` | CLI 框架 |
| `pydantic` | 数据验证 |
| `pydantic-settings` | 配置管理 |
| `python-dotenv` | .env 文件加载 |
| `tiktoken` | Token 计数（上下文预算管理） |

开发依赖：

| 包 | 用途 |
|----|------|
| `pytest` | 测试框架 |
| `pytest-asyncio` | 异步测试支持 |
