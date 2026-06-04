# Yanclaw 需求文档（SDD 前置版）

## 1. 文档目的

本文用于在进入 SDD（Software Design Document）前明确 Yanclaw 的产品需求、用户故事、验收标准与待确认问题。

本文根据当前代码反推，不把尚未从代码确认的产品决策写成既定事实。不确定项集中列在第 11 节，供后续 review 时确认。

## 2. 产品定位

Yanclaw 是一个面向高校导师信息采集、治理和推荐的本地智能体系统。

系统当前由四类能力组成：

1. Crawler：从高校公开网站采集学院、教师、院士和导师详情数据。
2. Human-Assisted Fetch：通过本地浏览器和 Tampermonkey 脚本协助抓取 WAF、跳转、动态页面等自动请求难以处理的网页。
3. Data Steward：对已采集的高校数据库做去重、质量审计，并在需要时生成补抓任务。
4. Recommender：把高校数据库构建成本地知识图谱，并基于用户文本或简历推荐学校、院系/方向和导师。

## 3. 用户角色

### 3.1 爬取操作员

负责启动爬取任务、安装并操作浏览器辅助脚本、处理页面跳转、登录/WAF、人机验证或无法访问页面。

### 3.2 数据维护者

负责检查采集结果质量、运行 dry-run 审计、确认删除重复数据或生成补抓任务。

### 3.3 申请者 / 咨询使用者

输入个人背景、研究兴趣、地点偏好或简历，获取可解释的学校、院系/方向和导师推荐。

### 3.4 开发者 / 维护者

负责扩展高校适配启发式、工具、字段、skills、数据模型、推荐逻辑和测试。

## 4. 范围

### 4.1 当前范围

- 采集中国高校公开网页上的导师和院士信息。
- 以高校为单位保存本地 SQLite 数据库。
- 支持从 `assets/websites.md` 中选择全部或部分高校运行。
- 支持按学院/院系名称模糊筛选目标 org unit。
- 默认使用人工辅助浏览器桥抓取页面。
- 支持新跑批备份旧数据库，支持 resume 模式复用已有进度和任务。
- 支持 Data Steward 对重复身份、院士/教授重复、核心字段缺失进行审计和可选修复。
- 支持由本地采集数据库构建知识图谱，并做导师推荐。
- 支持 OpenAI API 兼容接口，用于爬取解析、可选数据治理分类、可选用户画像解析。

### 4.2 当前非范围

- 不采集需要授权才能访问的非公开数据。
- 不以绕过权限控制为目标；人工辅助仅用于操作员自己可正常访问的公开页面。
- 不保证所有高校站点一次运行即可完整覆盖。
- 不提供多人协作、服务端账号体系或云端部署能力。
- 不把当前推荐结果定义为录取、学术评价或职业建议。

## 5. 关键用户故事

### 5.1 高校导师数据采集

**US-C01：选择高校并启动爬取**

作为爬取操作员，我希望通过 CLI 指定一个或多个高校名称启动爬取，以便只处理本次关注的学校。

验收标准：

- 可执行 `uv run yanclaw crawl --universities "北京航空航天大学"`。
- 未指定 `--universities` 时，系统按 `assets/websites.md` 中的目标列表处理。
- CLI 输出本次运行的 success / failed / skipped 统计。
- 若启动前 LLM 检查失败，默认阻止继续运行并提示配置问题。

**US-C02：全新跑批时保护旧数据**

作为数据维护者，我希望非 resume 跑批自动备份被选中高校的旧数据库，以便新跑批失败时仍能回看历史结果。

验收标准：

- 默认 fresh run 会把选中高校现有 DB 复制到 `data/universities/backup/<timestamp>/` 后再删除原 DB。
- 备份失败时，本次运行中止，不应删除原 DB。
- 仅备份本次选中的高校 DB，不影响未选中高校。

**US-C03：断点续爬**

作为爬取操作员，我希望使用 `--resume` 复用已有数据库、页面缓存和任务状态，以便中断后继续运行。

验收标准：

- `--resume` 不备份或删除已有高校 DB。
- 已标记 completed 且教授数量大于 0 的高校被跳过。
- 未完成、失败、无教授或缺少 DB 的高校继续爬取。
- pending / retry / stale in_progress 的 crawl_tasks 能被恢复处理。

**US-C04：按学院定向爬取**

作为爬取操作员，我希望用 `--org-units` 指定学院/院系名称，以便调试或补抓某些学院。

验收标准：

- 支持逗号分隔多个 org unit 名称。
- 使用可配置阈值做模糊匹配。
- 未匹配到目标时应尽早失败并说明原因。
- 目标学院全部找不到师资页时，可完成运行但应记录 warning 和 org unit 状态。

**US-C05：自动发现学院和师资页**

作为爬取操作员，我希望系统从大学主页自动发现学院列表、学院主页和师资页面，以减少手工配置路径。

验收标准：

- 系统识别学院/院系/研究所等 org unit 并写入 `org_units`。
- 系统按启发式和 LLM 共同寻找师资页。
- 低信息、登录、新闻、招聘、招生、党务等噪声页面应尽量过滤。
- 同域和可信子域链接可被跟踪，明显外部链接不被跟踪。

**US-C06：抽取导师列表与详情**

作为数据维护者，我希望系统从师资列表和详情页提取结构化导师信息，以便形成可检索的本地数据库。

验收标准：

- 至少保存姓名、大学、学院来源关系。
- 尽力提取职称、研究方向、邮箱、电话、内部主页、外部主页、履历、招生偏好、代表成果。
- 列表页和详情页使用不同字段严格度：详情页可补充富文本字段，列表页不应臆造详情字段。
- 退休/离退休/emeritus 等记录应被过滤。
- 院士应保存到 `academicians`，并与教授重复数据去重或合并。

**US-C07：流水线提取**

作为爬取操作员，我希望列表页提取任务能够异步排队处理，以便抓取和 LLM/DB 写入可以解耦。

验收标准：

- `PIPELINE_ENABLED=True` 时，页面内容写入 `crawl_tasks` 并由 LLM worker 与 DB worker 处理。
- 任务按 `source_url + org_unit_name + page_hash` 去重。
- LLM JSON 参数非法时按配置重试。
- 失败样本写入 `crawl_extraction_failures`，不阻塞整体流程。
- SQLite 写入默认单 worker，避免常见写锁问题。

### 5.2 人工辅助抓取

**US-H01：浏览器接收抓取任务**

作为爬取操作员，我希望本地浏览器脚本能从 Yanclaw 后端领取抓取任务，以便用真实浏览器打开网页并提交 HTML。

验收标准：

- Crawler 默认启动 `HumanFetcherBridge`，监听 `127.0.0.1:21520`。
- Tampermonkey 脚本通过 `/api/jobs/next` 拉取任务。
- 任务包含目标 URL、大学、当前阶段、意图、学院、来源 URL 等上下文。
- 提交成功后后端将 HTML 转为文本并提取链接信号。

**US-H02：人工处理异常网页**

作为爬取操作员，我希望能跳过、标记失败或覆盖目标 URL，以便处理跳转错误、登录页、WAF、页面不存在等情况。

验收标准：

- 支持 complete / fail / skip / override URL。
- complete 必须提交 HTML。
- fail/skip 让 fetch 返回空结果和 block reason，Agent 继续处理后续任务。
- 单个 job 超时后不应永久阻塞运行。

**US-H03：查看运行状态**

作为爬取操作员，我希望在浏览器脚本中查看队列和 Agent 状态，以便判断当前任务和整体进度。

验收标准：

- `/api/status` 返回队列统计、当前 job、pending decision 和 Agent 状态。
- CORS 允许本地脚本访问 API。
- 同一浏览器多标签下应通过 instance lock 避免多个主实例重复处理任务。

### 5.3 数据治理

**US-S01：dry-run 审计数据库**

作为数据维护者，我希望先以 dry-run 方式审计高校 DB，以便在不修改数据的前提下了解质量问题。

验收标准：

- 可执行 `uv run yanclaw steward run`。
- 支持按高校名称、名称文件或 DB root 选择目标 DB。
- dry-run 写入 `steward_runs` 和 `data_quality_audits`，但不删除教授记录、不生成补抓任务。
- 输出重复数量、缺失字段审计数量、补抓任务数量、目标数量。

**US-S02：应用治理修复**

作为数据维护者，我希望在确认后用 `--apply` 删除或合并重复数据，并生成必要的补抓任务。

验收标准：

- 院士与教授重复时，教授重复记录可删除或合并到院士。
- 同一身份因姓名污染、主页重复等导致的教授重复应被合并。
- 缺少 research_areas 或 bio 且判断为 crawl_failure 时，生成 recrawl task。
- 所有变更前后证据和动作写入审计表。

**US-S03：解释缺失字段原因**

作为数据维护者，我希望系统区分“抓取失败”和“网站未公开”，以便只对有必要的记录生成补抓任务。

验收标准：

- 静态规则根据 crawl_logs、extraction failures、source_url/homepage 判断原因。
- 可选 `--llm-enabled` 对 uncertain 样本做 LLM 分类。
- LLM 分类只能输出 crawl_failure / site_missing / uncertain。
- 与证据矛盾的 LLM 分类应降级为 uncertain。

**US-S04：备份对比审计**

作为数据维护者，我希望只读比较当前 DB 与最新备份 DB 的记录数量，以便评估 fresh run 影响。

验收标准：

- `--include-backup-audit` 只能在只读模式使用，不能与 `--apply` 组合。
- 返回当前 professors/academicians 计数与最新备份计数。

### 5.4 知识图谱与推荐

**US-R01：构建本地知识图谱**

作为数据维护者，我希望把一个或多个高校 DB 构建成本地知识图谱，以便后续推荐查询。

验收标准：

- 可执行 `uv run yanclaw graph build`。
- 支持按高校名称或 DB root 选择来源。
- 支持 `--rebuild` 清空后重建。
- 增量模式根据源 DB 内容 hash 跳过未变化来源。
- 图谱包含 university、org_unit、professor、concept 节点，以及 HAS_ORG_UNIT、AFFILIATED_WITH、RESEARCHES、RECRUITS_FOR、HAS_TITLE 等关系。

**US-R02：从文本或简历生成推荐**

作为申请者，我希望输入研究兴趣或简历，获取学校、院系/方向和导师推荐。

验收标准：

- 可执行 `uv run yanclaw recommend --text "..."`。
- 支持输入 `.txt`、`.md`、`.pdf`、`.docx` 文件。
- text 和 file 必须二选一。
- 输出包含 schools、org_units、professors 三个层级。
- 每条推荐包含 score、matched_terms、evidence_urls、reasons。
- 支持 `--json` 输出稳定 JSON。

**US-R03：无 LLM 时仍可推荐**

作为申请者，我希望即使没有配置 LLM API key，也能得到基于本地文本匹配的推荐。

验收标准：

- 没有 API key 时，用户画像解析退回本地 parser。
- 本地 parser 能提取常见研究兴趣、地点、博士/硕士目标、导师职称偏好。
- 推荐只依赖本地知识图谱和词项匹配，不访问网络。

**US-R04：自动构建缺失图谱**

作为申请者，我希望推荐时如果知识图谱不存在或为空，系统自动构建，以减少前置操作。

验收标准：

- 默认 auto-build 开启。
- `--no-auto-build` 可关闭自动构建。
- 图谱为空且禁用 auto-build 时，返回空推荐而不是异常崩溃。

### 5.5 Skills 与运行时

**US-T01：管理 crawler skills**

作为开发者，我希望查看、diff、history 和 rollback crawler skills，以便管理 LLM 提取指令的版本。

验收标准：

- 支持 `yanclaw skills list / history / diff / rollback`。
- skills 文件位于 `src/agents/crawler/skills`。
- skill 历史版本存储在 meta DB 中。
- 当前工具集保持最小化，不允许 crawler 运行时通过 tool 自行创建或修改 skill。

**US-T02：检查 LLM 配置**

作为操作员，我希望在长跑批前检查 LLM 连接，以便尽早发现 endpoint、model 或 key 配置错误。

验收标准：

- `yanclaw llm-check` 输出 base_url、model、key 是否设置、timeout、temperature、top_p、seed。
- 检查请求要求模型返回 `ok`。
- crawl 默认先做 llm-check，可通过 `--skip-llm-check` 跳过。

**US-T03：管理 cookie 文件**

作为操作员，我希望导入、查看和清理高校域名 cookie 文件，以便辅助人工或后续抓取场景。

验收标准：

- 支持 `yanclaw cookie import <url> <file.json>`。
- cookie 文件必须是 JSON 数组。
- 支持 `cookie list` 和 `cookie clear <url>`。
- cookie 文件按 eTLD+1 / site root 归档。

## 6. 数据需求

### 6.1 高校目标

高校目标从 `assets/websites.md` 读取，当前解析逻辑接受 CSV-style header，核心字段为：

- name / university
- url
- location

### 6.2 每高校独立 DB

每所高校一个 SQLite DB，路径为：

`data/universities/<site-root>.db`

示例：

- `https://www.buaa.edu.cn/` -> `data/universities/buaa.edu.cn.db`

### 6.3 核心实体

- `university_meta`：高校名称、起始 URL、地点、crawl_status。
- `org_units`：学院、院系、研究所等学术组织。
- `professors`：普通教师事实表。
- `academicians`：院士事实表。
- `professor_affiliations`：教师与 org unit 的多对多关系。
- `crawl_logs`：fetch 成功/失败/跳过记录。
- `crawl_page_cache`：页面快照、链接和链接信号缓存。
- `crawl_tasks`：提取任务队列与断点续爬任务。
- `crawl_extraction_failures`：LLM/解析失败样本。
- `steward_runs` / `data_quality_audits`：数据治理运行和审计记录。

### 6.4 推荐图谱 DB

知识图谱默认存储在 `data/knowledge_graph.db`，核心表包括：

- `kg_sources`
- `kg_nodes`
- `kg_edges`
- `kg_documents`
- `kg_terms`
- `kg_build_runs`

## 7. 功能需求汇总

### 7.1 CLI

系统应提供以下命令：

- `yanclaw crawl`
- `yanclaw llm-check`
- `yanclaw cookie import/list/clear`
- `yanclaw skills list/history/diff/rollback`
- `yanclaw steward run`
- `yanclaw graph build`
- `yanclaw recommend`

### 7.2 Crawler 状态流

Crawler 应至少支持以下状态流：

```text
DISCOVER_ORG_UNIT_PAGES -> EXTRACT_ORG_UNITS -> FIND_FACULTY_PAGES -> EXTRACT_PROFESSORS -> DONE
```

默认人工辅助模式下，系统应以 streaming 方式按学院执行“找师资页 -> 抽取老师 -> 详情富化”，而不是等所有学院师资页全部找到后再统一抽取。

### 7.3 URL 策略

系统应：

- 对学院页、师资页、教师详情页分别评分和分类。
- 过滤明显噪声、登录、招聘、招生、新闻、非学术平台链接。
- 避免把分页、分类页误判为教师详情页。
- 对特殊高校 CMS 模式保留可扩展启发式。
- 对同域和高校子域做合理允许，对外部站点默认拒绝。

### 7.4 数据清洗与去重

系统应：

- 规范化姓名、职称、研究方向、成果、空值文本。
- 从职称中推断博士生导师/硕士生导师等招生偏好。
- 区分内部 profile homepage 和外部个人主页 external_link。
- 按姓名 key、邮箱、主页、院士匹配等方式去重。
- 保留教师与多个 org unit 的 affiliation。

### 7.5 错误与恢复

系统应：

- 单页面失败不终止整个高校。
- 单高校失败不终止其他高校。
- 支持 per-university timeout 和 total run timeout。
- 对 LLM 非法 JSON 做有限重试。
- 对任务失败写入审计或 failure 表。
- 在 resume 模式下恢复 pending/retry/stale in_progress 任务。

## 8. 非功能需求

### 8.1 本地优先

系统应默认在本地运行，数据写入本地 SQLite。除配置的 LLM API 外，不应依赖外部服务。

### 8.2 可观测性

系统应输出：

- 控制台 INFO 摘要。
- `logs/crawl_<timestamp>.log` DEBUG 级日志。
- 爬取配置、任务分派、fetch、LLM 请求/响应摘要、tool 调用、数据库写入、跳过原因、pipeline 统计。

### 8.3 可配置性

系统应通过 `.env` 或环境变量 `YANCLAW_` 前缀配置：

- LLM endpoint、key、model、timeout、temperature、top_p、seed。
- 并发数、超时、日志目录。
- websites 路径、高校 DB 目录、知识图谱 DB 路径。
- human bridge host/port/job timeout。
- detail enrich、pipeline worker、queue cap、task recovery。
- 推荐返回数量。

### 8.4 性能与资源

- 多高校并发受 `MAX_CONCURRENCY` 控制。
- 人工辅助抓取吞吐主要受操作员浏览器操作速度限制。
- SQLite 写入应避免过多并发写 worker。
- 大页面应受 token 预算控制，不能无限发送到 LLM。

### 8.5 安全与合规

- 仅采集公开学术页面。
- 本地 human API 默认监听 `127.0.0.1`。
- 不保存 LLM API key 到采集数据库。
- 不应故意绕过授权、验证码或登录权限。
- 推荐结果必须保留 evidence URLs 和 reasons，便于人工核查。

### 8.6 测试

与需求相关的回归测试至少覆盖：

- Crawler 状态机、resume、pipeline、URL 启发式、DB upsert、human bridge/server。
- Steward dry-run/apply、重复检测、缺失字段审计、补抓任务。
- Graph build、增量跳过、recommend JSON/text/file 输入。
- Runtime LLM、skills、context、database。

## 9. 主要质量指标

以下指标目前作为需求方向，具体阈值待确认：

- 高校跑批完成率。
- 目标学院匹配准确率。
- 师资页识别准确率。
- 教师详情页召回率。
- 教授/院士重复率。
- 核心字段完整率（research_areas、bio、homepage、email 等）。
- Steward dry-run 与 apply 的可解释审计覆盖率。
- 推荐结果人工可接受率。

## 10. 运行示例

```bash
uv run yanclaw llm-check
uv run yanclaw crawl --universities "北京航空航天大学"
uv run yanclaw crawl --universities "北京航空航天大学" --org-units "软件学院" --resume
uv run yanclaw steward run --universities "北京航空航天大学"
uv run yanclaw steward run --universities "北京航空航天大学" --apply
uv run yanclaw graph build --universities "北京航空航天大学" --rebuild
uv run yanclaw recommend --text "我想申请北京的人工智能方向博士，希望找博士生导师。" --json
```

## 11. 待确认问题

以下问题无法从现有代码唯一确定，需要产品 review 后确认：

1. Yanclaw 的 SDD 范围是否只覆盖 crawler，还是覆盖 crawler + steward + recommender 的完整系统？
2. `src/agents/recommender/` 当前是未跟踪文件，但 CLI 已引用。推荐系统是否属于正式需求范围？
3. 是否继续把 `docs/crawler/requirements.md` 作为项目总需求文档，还是应拆分为 `docs/requirements.md` 与 `docs/crawler/requirements.md`？
4. 推荐功能的目标用户是谁：考研/保研/申博申请者、咨询顾问、还是内部数据检索人员？
5. 推荐评分是否需要可校准的业务指标，还是保持当前启发式文本匹配即可？
6. 采集字段是否需要扩展为结构化论文、项目、招生名额、招生年份、学科代码等？
7. “公开页面”的边界如何定义：操作员手动登录后可见但未公开索引的页面是否允许采集？
8. Human-assisted 模式是否应继续作为唯一默认 fetcher，还是需要恢复/新增纯 HTTP 自动 fetcher？
9. 是否需要为高校站点设置 robots.txt、请求频率或人工操作规范的显式合规要求？
10. 新跑批默认删除并重建选中 DB 是否符合预期，还是应默认 resume、显式指定 fresh？
11. Data Steward `--apply` 的删除策略是否需要二次确认、备份强制检查或软删除？
12. 是否需要导出能力（CSV/JSON/Excel）供人工 review 或外部系统使用？
13. 是否需要用户界面，而不只是 CLI + Tampermonkey 面板？
14. 是否需要在推荐结果中隐藏邮箱/电话等联系信息，或保留完整公开字段？
15. 是否需要定义每所高校“采集完成”的最低标准，例如教授数量、学院覆盖率、详情字段完整率？

## 12. SDD 输入摘要

后续 SDD 至少需要展开：

- Agent 状态机与 streaming/pipeline 时序。
- Human bridge API、脚本状态机和异常处理。
- 每高校 DB schema、迁移策略和数据生命周期。
- URL 启发式模块边界与测试策略。
- LLM prompt、tool calling、JSON 失败恢复和 token 预算。
- Data Steward 的审计模型、apply 事务边界和补抓任务生成规则。
- Knowledge graph schema、增量构建、推荐评分模型和解释生成。
- 配置、日志、测试、部署和操作手册。
