# Human-Assisted Crawler — 设计文档

## 1. 问题背景

现有 CrawlerAgent 通过 httpx / curl_cffi / Playwright / Crawl4AI 等后端自动抓取高校网页。
部分高校部署了严格的 WAF（Web Application Firewall），即使切换后端、注入 cookies 仍无法绕过。

核心矛盾：**自动化抓取被 WAF 拦截，但人工用浏览器访问完全正常。**

## 2. 解决方案概述

引入 **Human-Assisted Fetcher** 模式：

- Python 后端（决策者）：CrawlerAgent 照常运行 LLM 决策循环，但当需要抓取页面时，
  不再自己发 HTTP 请求，而是将 **fetch job** 推入队列，等待人工完成。
- 油猴脚本（执行者）：运行在人工打开的 GUI 浏览器中，轮询后端拉取任务，
  展示 Agent 的决策意图，引导人工导航到目标页面，提取页面内容后回传。

```
┌─────────────────────────────────────────────────────────┐
│  Python Backend (决策者)                                  │
│                                                          │
│  CrawlerAgent ──► HumanFetcherBridge ──► Job Queue       │
│       ▲                                     │            │
│       │              HTTP API               │            │
│       │         ┌───────────────────┐       │            │
│       └─────────│  GET  /jobs/next  │◄──────┘            │
│                 │  POST /jobs/:id   │                    │
│                 └───────────────────┘                    │
└─────────────────────────┬───────────────────────────────┘
                          │ HTTP (localhost)
                          ▼
┌─────────────────────────────────────────────────────────┐
│  Tampermonkey Script (执行者 + 人工辅助)                   │
│                                                          │
│  ┌──────────────────────────────────────────────┐       │
│  │  浮动面板 UI                                   │       │
│  │  ┌─ 当前任务: 抓取 https://cs.pku.edu.cn/... │       │
│  │  │  Agent 意图: 寻找计算机学院师资列表页       │       │
│  │  │  状态: ⏳ 等待人工导航                      │       │
│  │  │                                            │       │
│  │  │  [📋 复制URL] [✅ 提交当前页] [⏭ 跳过]    │       │
│  │  │  [✏️ 手动输入URL] [⏸ 暂停]                 │       │
│  │  └────────────────────────────────────────────│       │
│  └──────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────┘
```

## 3. 架构设计

### 3.1 组件关系

```
src/agents/crawler/
├── human_server.py          # HTTP API 服务器 (aiohttp)
├── human_bridge.py          # HumanFetcherBridge: 实现 Fetcher 接口，内部通过 job queue 等待人工
├── human_models.py          # FetchJob 数据模型 + JobQueue
└── ...

userscripts/
└── yanclaw-assistant.user.js  # Tampermonkey 油猴脚本
```

### 3.2 与现有系统的集成点

HumanFetcherBridge 是当前唯一支持的运行时 fetcher，并实现 `fetch(url) -> FetchResult`。
集成方式：

1. `CrawlDispatcher` 的默认 fetcher factory 直接创建 HumanFetcherBridge
2. HumanFetcherBridge 内部启动 aiohttp HTTP server，暴露 job queue API
3. CrawlerAgent 调用 `fetcher.fetch(url)` 时，bridge 创建 FetchJob 并 `await` 直到人工完成

```
CrawlerAgent.fetch(url)
    │
    ▼
HumanFetcherBridge.fetch(url)
    │
    ├── 1. 创建 FetchJob(url, context=agent决策信息)
    ├── 2. 放入 JobQueue
    ├── 3. await job.done_event  ← 阻塞等待
    │                               │
    │   (油猴脚本 GET /jobs/next)   │
    │   (人工导航到页面)             │
    │   (油猴脚本 POST /jobs/:id)   │
    │                               │
    ├── 4. job.done_event.set()  ←──┘
    └── 5. 返回 FetchResult(text, links, status_code)
```

## 4. 数据模型

### 4.1 FetchJob

```python
class FetchJobStatus(str, Enum):
    PENDING   = "pending"    # 等待人工领取
    ASSIGNED  = "assigned"   # 油猴脚本已领取，人工操作中
    COMPLETED = "completed"  # 人工已提交结果
    FAILED    = "failed"     # 人工标记失败/超时
    SKIPPED   = "skipped"    # 人工跳过

class FetchJob:
    id: str                  # UUID
    url: str                 # 目标 URL
    status: FetchJobStatus
    context: JobContext       # Agent 决策上下文（展示给人工）
    result: FetchResult | None
    created_at: datetime
    assigned_at: datetime | None
    completed_at: datetime | None
    timeout_seconds: float   # 单任务超时（默认 300s）

class JobContext:
    university_name: str     # 当前大学
    agent_state: str         # Agent 状态机阶段
    intent: str              # Agent 意图描述（LLM 生成）
    parent_url: str          # 从哪个页面发现的这个 URL
    depth: int               # 探索深度
    org_unit_name: str       # 当前学院名（如有）
    hints: list[str]         # 给人工的提示
```

### 4.2 JobQueue（内存队列）

不需要持久化——job 的生命周期与 CrawlerAgent 运行周期一致。

```python
class JobQueue:
    _pending: asyncio.Queue[FetchJob]
    _jobs: dict[str, FetchJob]       # id → job，用于状态查询和结果提交
    _active_count: int               # 当前 assigned 但未完成的任务数

    async def submit(job: FetchJob) -> None
    async def next() -> FetchJob | None
    async def complete(job_id: str, result: FetchResult) -> None
    async def fail(job_id: str, message: str) -> None
    async def skip(job_id: str) -> None
    def stats() -> dict                # pending/assigned/completed/failed 计数
```

## 5. HTTP API 设计

aiohttp 轻量 HTTP 服务器，监听 `localhost:21520`（端口可配置）。

### 5.1 端点列表

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/jobs/next` | 领取下一个待处理任务 |
| POST | `/api/jobs/{id}/complete` | 提交任务结果（页面内容） |
| POST | `/api/jobs/{id}/fail` | 标记任务失败 |
| POST | `/api/jobs/{id}/skip` | 跳过任务 |
| GET | `/api/jobs/{id}` | 查询单个任务状态 |
| GET | `/api/status` | 队列整体状态 + Agent 运行状态 |
| POST | `/api/jobs/{id}/override` | 人工覆盖 URL（重定向到其他页面） |

### 5.2 API 详细定义

#### GET /api/jobs/next

领取下一个 pending 任务，将其状态改为 assigned。

Response 200:
```json
{
  "id": "uuid",
  "url": "https://cs.pku.edu.cn/szdw/index.htm",
  "context": {
    "university_name": "北京大学",
    "agent_state": "FIND_FACULTY_PAGES",
    "intent": "寻找计算机学院的师资队伍列表页",
    "parent_url": "https://www.pku.edu.cn/",
    "depth": 2,
    "org_unit_name": "信息科学技术学院",
    "hints": ["页面应包含教师姓名列表", "可能有分页"]
  },
  "created_at": "2026-04-30T22:00:00+08:00",
  "timeout_seconds": 300
}
```

Response 204: 无待处理任务。

#### POST /api/jobs/{id}/complete

```json
{
  "html": "<html>...</html>",
  "url": "https://cs.pku.edu.cn/szdw/index.htm",
  "title": "师资队伍 - 北京大学计算机学院"
}
```

油猴脚本提交当前页面的完整 HTML。后端负责 HTML→text 转换和链接提取
（复用现有 `Fetcher` 的 `_html_to_text` 和 `_extract_links` 逻辑）。

Response 200:
```json
{
  "status": "completed",
  "next_job": { ... } | null
}
```

返回中附带下一个任务（如有），减少一次轮询。

#### POST /api/jobs/{id}/fail

```json
{
  "message": "页面需要登录"
}
```

#### POST /api/jobs/{id}/skip

无 body。Agent 收到 skip 后按照现有逻辑处理（等同于 fetch 返回空内容）。

#### POST /api/jobs/{id}/override

人工发现 Agent 给的 URL 不对，手动指定正确 URL：

```json
{
  "new_url": "https://cs.pku.edu.cn/szll/index.htm"
}
```

后端更新 job 的 target URL，油猴脚本刷新显示。

#### GET /api/status

```json
{
  "queue": {
    "pending": 3,
    "assigned": 1,
    "completed": 12,
    "failed": 0,
    "skipped": 1
  },
  "agent": {
    "university_name": "北京大学",
    "state": "EXTRACT_PROFESSORS",
    "visited_count": 15,
    "saved_professors": 42
  },
  "server_uptime_seconds": 1234
}
```

### 5.3 CORS

所有响应添加 `Access-Control-Allow-Origin: *`，允许油猴脚本跨域访问。

## 6. HumanFetcherBridge 设计

### 6.1 接口

```python
class HumanFetcherBridge:
    """Fetcher 接口的人工辅助实现。"""

    def __init__(self, *, host: str, port: int, job_timeout: float):
        self.queue = JobQueue()
        self._server: aiohttp.web.AppRunner  # HTTP server

    async def __aenter__(self) -> "HumanFetcherBridge":
        # 启动 HTTP server（后台 task）
        ...

    async def __aexit__(self, *args) -> None:
        # 关闭 HTTP server
        ...

    async def fetch(self, url: str, *, context: JobContext | None = None) -> FetchResult:
        # 1. 创建 FetchJob
        # 2. 放入队列
        # 3. await job.done_event (with timeout)
        # 4. 返回 FetchResult
        ...

    # 以下方法保持与 Fetcher 兼容
    @staticmethod
    def filter_same_domain(urls, base_url) -> list[str]:
        return Fetcher.filter_same_domain(urls, base_url)
```

### 6.2 超时处理

每个 FetchJob 有独立超时（默认 300s）。超时后：
- job 状态变为 `failed`
- `fetch()` 返回 `FetchResult(url=url, text="", links=[], status_code=0, block_reason="human_timeout")`
- Agent 按现有逻辑处理失败（跳过，继续下一个）

### 6.3 Agent 上下文传递

CrawlerAgent 调用 `fetcher.fetch(url)` 时，HumanFetcherBridge 需要知道 Agent 的当前意图。
通过在 `fetch()` 方法中接受可选的 `context` 参数实现。

为了不修改 `Fetcher` 基类接口，bridge 额外提供 `set_context()` 方法，
Agent 在每次 fetch 前调用：

```python
# CrawlerAgent 中（仅在 human 模式下）
if hasattr(self.fetcher, 'set_context'):
    self.fetcher.set_context(JobContext(
        university_name=self.university_name,
        agent_state=self.state.value,
        intent="寻找计算机学院师资列表页",
        parent_url=current_url,
        depth=depth,
        org_unit_name=org_unit.name,
    ))
result = await self.fetcher.fetch(url)
```

## 7. 油猴脚本 UI 设计

### 7.1 浮动面板

固定在浏览器右下角的可拖拽面板，包含：

```
┌─────────────────────────────────────────────┐
│  🦀 Yanclaw Assistant          [─] [×]      │
├─────────────────────────────────────────────┤
│  📊 队列: 3 待处理 | 1 进行中 | 12 已完成    │
├─────────────────────────────────────────────┤
│  🎯 当前任务 #7                              │
│  大学: 北京大学                               │
│  阶段: FIND_FACULTY_PAGES                    │
│  意图: 寻找信息科学技术学院的师资列表页        │
│  目标URL:                                    │
│  https://cs.pku.edu.cn/szdw/index.htm       │
│  来源: https://www.pku.edu.cn/              │
│  深度: 2/4                                   │
│                                              │
│  💡 提示:                                    │
│  • 页面应包含教师姓名列表                     │
│  • 可能有分页                                │
│                                              │
│  ┌──────────────────────────────────────┐   │
│  │ [📋 复制URL] [🔗 打开URL]            │   │
│  │ [✅ 提交当前页面] [⏭ 跳过]           │   │
│  │ [✏️ 手动输入URL]  [❌ 标记失败]       │   │
│  └──────────────────────────────────────┘   │
├─────────────────────────────────────────────┤
│  📜 历史记录 (最近5条)                       │
│  ✅ #6 szdw/index.htm          3s ago       │
│  ✅ #5 jgsz/index.htm          45s ago      │
│  ⏭ #4 news/index.htm          1m ago       │
└─────────────────────────────────────────────┘
```

### 7.2 交互流程

1. **自动轮询**: 脚本每 2s 轮询 `GET /api/jobs/next`
2. **任务到达**: 面板显示任务详情，高亮目标 URL
3. **人工导航**: 用户点击"打开URL"或手动在浏览器中导航
4. **URL 匹配检测**: 脚本检测当前页面 URL 是否匹配目标 URL（模糊匹配，忽略 trailing slash 和 fragment）
5. **自动提交提示**: URL 匹配时，面板显示"检测到目标页面，是否提交？"
6. **手动提交**: 用户点击"提交当前页面"，脚本抓取 `document.documentElement.outerHTML` 并 POST
7. **下一任务**: 提交成功后自动拉取下一个任务

### 7.3 人工介入场景

| 场景 | 操作 |
|------|------|
| URL 正确，页面正常 | 导航到 URL → 自动检测 → 提交 |
| URL 被重定向到登录页 | 手动登录后 → 提交当前页面 |
| URL 404 但知道正确地址 | 手动输入URL → 导航 → 提交 |
| 页面需要点击展开/翻页 | 手动操作后 → 提交 |
| 页面确实无法访问 | 标记失败 |
| 任务不相关 | 跳过 |
| Agent 给的 URL 错误 | 使用"手动输入URL"覆盖 |

### 7.4 自动模式（可选）

面板提供"自动模式"开关：
- 开启后，脚本自动在当前 tab 中 `window.location.href = job.url`
- 页面加载完成后等待 2s（让 JS 渲染完成），自动提交
- 遇到 WAF 验证码时暂停，等待人工处理后继续

## 8. 配置扩展

### 8.1 CrawlerSettings 新增

```python
# human-assisted fetcher
human_server_host: str = "127.0.0.1"
human_server_port: int = 21520
human_job_timeout_seconds: float = 300.0
```

### 8.2 CLI 新增

```
yanclaw crawl --fetcher-backend human [--universities ...]
```

使用 `human` 后端时，CLI 启动后打印：

```
Human-assisted mode. Open your browser with Tampermonkey script.
API server listening on http://127.0.0.1:21520
Waiting for human operator...
```

### 8.3 油猴脚本配置

脚本顶部的 `@grant` 和用户可配置项：

```javascript
// ==UserScript==
// @name         Yanclaw Assistant
// @namespace    https://github.com/yanclaw
// @version      1.0.0
// @description  Human-assisted crawler frontend for Yanclaw
// @match        *://*.edu.cn/*
// @grant        GM_xmlhttpRequest
// @grant        GM_addStyle
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

const CONFIG = {
    API_BASE: 'http://127.0.0.1:21520/api',
    POLL_INTERVAL: 2000,      // ms
    AUTO_SUBMIT_DELAY: 2000,  // ms, 自动模式下页面加载后等待时间
};
```

## 9. 安全考虑

- HTTP server 仅监听 `127.0.0.1`，不暴露到网络
- 无认证（本地单用户场景），如需多人协作可后续加 token
- 油猴脚本通过 `GM_xmlhttpRequest` 绕过浏览器 CORS 限制
- 不传输任何敏感信息（仅页面 HTML 内容）

## 10. 文件结构

```
src/agents/crawler/
├── human_models.py      # FetchJob, JobContext, FetchJobStatus, JobQueue
├── human_bridge.py      # HumanFetcherBridge (Fetcher 接口实现)
├── human_server.py      # aiohttp HTTP API handlers + server setup
├── ...

userscripts/
└── yanclaw-assistant.user.js

tests/
├── unit/crawler/
│   ├── test_human_models.py
│   └── test_human_bridge.py
└── integration/crawler/
    └── test_human_server.py
```

## 11. 实现计划

### Phase 1: 后端核心
1. `human_models.py` — FetchJob + JobQueue
2. `human_bridge.py` — HumanFetcherBridge
3. `human_server.py` — HTTP API

### Phase 2: 前端脚本
4. `yanclaw-assistant.user.js` — 完整油猴脚本

### Phase 3: 集成
5. `config.py` — 新增配置项
6. `dispatcher.py` — 新增 human fetcher factory
7. `cli.py` — 新增 human backend choice

### Phase 4: 测试
8. 单元测试 + 集成测试

## 12. 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 通信方式 | HTTP 轮询 | 最简单，油猴脚本原生支持 GM_xmlhttpRequest，无需 WebSocket |
| 队列存储 | 内存 | Job 生命周期与 Agent 运行一致，无需持久化 |
| HTTP 框架 | aiohttp | 项目已是 async 架构，aiohttp 轻量且与 asyncio 原生集成 |
| HTML 解析位置 | 后端 | 复用现有 Fetcher 的 html2text + link extraction 逻辑 |
| Agent 修改量 | 最小化 | Bridge 实现 Fetcher 接口，Agent 无需感知 human 模式 |
| 脚本 @match | `*.edu.cn/*` | 仅在高校网站上激活，不影响其他浏览 |
| 端口 | 21520 | 避开常用端口，不易冲突 |
