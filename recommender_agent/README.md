# LightGraphRec AI 导师推荐系统

LightGraphRec 是 `recommender_agent` 的导师推荐 MVP。系统从 `raw_data/` 下登记的数据源导入教师数据，写入统一业务库 `data/app.db`，再基于 SQLite、ChromaDB、NetworkX 和 LLM 组合完成导师召回、排序与推荐理由生成。

当前版本已经覆盖 `项目规划.md` 的核心演示路径：数据导入、FastAPI 后端、Streamlit 前端、结构化召回、向量索引、知识图谱边构建、推荐接口和基础测试。它还不是规划文档里的完整终态：Repository 层、Engine 层、完整图谱路径解释、CI/CD 和隔离测试夹具仍属于后续完善项。

## 当前可用能力

- 后端 API：FastAPI，入口 `app.main:app`，默认端口 `8000`。
- 前端 Demo：Streamlit，入口 `frontend/streamlit_app.py`，默认端口 `8501`。
- 数据源：`raw_data/*.db` 原始教师库，通过 `configs/sources.yaml` 登记。
- 业务库：`data/app.db`，包含 `items`、`org_units`、`graph_edges` 等推荐运行表。
- 数据准备：`app.jobs.import_dataset.import_all_sources()` 将已启用数据源导入 `Item`，并归档 `raw_records`。
- 图谱构建：`app.jobs.build_graph` 从统一 `items` 构建 `graph_edges`。
- 向量索引：`app.jobs.build_vector_index` 将统一 `items` 写入 `data/chroma`。
- 一键重建：`app.jobs.rebuild_all` 执行导入、图谱和向量索引重建。
- 推荐接口：`POST /api/v1/recommend` 返回需求解析、匹配等级、证据、限制说明、追问建议和图谱路径。
- 追问接口：`POST /api/v1/recommend/follow-up` 基于上一轮 `request_id` 继续收窄推荐条件。
- 管理接口：`POST /api/v1/admin/import` 会导入数据并重建图谱/向量索引。

## 与项目规划的对齐情况

| 模块 | 状态 | 说明 |
|---|---|---|
| FastAPI 基础框架 | 已实现 | `/health`、统一响应、API 路由可用。 |
| 数据导入 | 已实现 | `raw_data/*.db -> data/app.db`，使用复合 `external_id` 防止多源主键冲突。 |
| 结构化召回 | 已实现 | 无过滤时可从活跃 `items` 返回候选。 |
| ChromaDB 向量索引 | 已实现 | 可构建 `professors_vector` collection。 |
| NetworkX 图谱 | 部分实现 | 已构建来源、学院、研究方向和相似研究边；复杂合作路径仍较简单。 |
| 推荐排序 | 已实现 MVP | 按语义、图谱、画像、热度权重融合。 |
| LLM 意图/理由 | 部分实现 | 支持 DeepSeek/OpenAI 风格接口和模板降级；成本控制较简单。 |
| Streamlit 前端 | 已实现 MVP | 可输入查询、查看推荐结果和耗时。 |
| Repository/Engine 分层 | 未完全实现 | 当前 Service 直接访问 SQLModel，未完全按规划拆 Repository/Engine。 |
| CI/CD 与 Docker | 部分实现 | Dockerfile/Compose 存在，但本地推荐优先用 `uv` 运行；Docker 构建依赖安装较慢。 |
| 测试 | 部分实现 | 排序/LLM/模型/API 测试存在；部分测试依赖真实 `data/app.db`，隔离性待加强。 |

## 环境准备

推荐使用 `uv` 和 Doppler。真实密钥不要写入仓库。

必需工具：

- Python 3.10+，推荐 3.11
- `uv`
- Doppler CLI 或 Doppler MCP 访问权限
- 将原始库放入 `raw_data/`，并在 `configs/sources.yaml` 登记

配置来源：

- 示例配置见 `.env.example`
- 当前真实配置建议使用 Doppler：project `yanclaw`，config `dev_personal`

关键环境变量：

```env
APP_NAME=LightGraphRec
APP_ENV=local
DATABASE_URL=sqlite:///data/app.db
SCU_SOURCE_DB=raw_data/scu.edu.cn.db
SOURCE_CONFIG=configs/sources.yaml
SOURCE_DB_DIR=raw_data
SOURCE_DB_GLOB=*.db
SOURCE_DBS=
DATA_DIR=data
PIPELINE_REPORT_DIR=data/pipeline_reports
CHROMA_PATH=data/chroma
CHROMA_COLLECTION=professors_vector
LLM_PROVIDER=deepseek
LLM_API_KEY=
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
ENABLE_LLM=true
ENABLE_VECTOR=true
ENABLE_GRAPH=true
ENABLE_DOMAIN_GATE=true
DOMAIN_GATE_MODE=soft
DOMAIN_ONTOLOGY_PATH=configs/discipline_ontology.yaml
GRAPH_DIFFUSION_ENABLED=true
GRAPH_RELATION_WEIGHTS=
ENABLE_RERANKER=false
RERANK_PROVIDER=heuristic
RERANK_MODEL=
RERANK_BASE_URL=
RERANK_API_KEY=
```

外部 reranking 默认关闭，`ENABLE_RERANKER=false` 时本地运行和测试不需要配置任何 rerank provider。后续如需接入外部 reranker，请只在本机 `.env`、Doppler 或其他密钥管理工具中设置 `ENABLE_RERANKER=true`、`RERANK_PROVIDER`、`RERANK_MODEL`、`RERANK_BASE_URL` 和 `RERANK_API_KEY`；不要把真实 key 写入 `.env.example` 或提交到仓库。

## 安装依赖

在 `recommender_agent` 目录执行：

```bash
uv pip install -r requirements.txt
uv pip install -e ".[dev]"
```

如果你不需要运行测试，可以只安装 `requirements.txt`。

## 准备推荐数据

首次运行前必须把原始库放入 `raw_data/`，确认 `configs/sources.yaml` 已启用对应数据源，然后导入业务库并构建图谱/向量索引。

推荐一条命令完成全量重建：

```bash
doppler run --project yanclaw --config dev_personal -- uv run python -m app.jobs.rebuild_all
```

如只想验证导入和图谱，不重建向量索引：

```bash
doppler run --project yanclaw --config dev_personal -- uv run python -m app.jobs.rebuild_all --skip-vector
```

也可以在后端启动后调用管理接口：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/admin/import
```

注意：如果 `data/app.db` 为空，前端会正常启动，但推荐结果会是 0。

## 启动后端

```bash
doppler run --project yanclaw --config dev_personal -- uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

验证：

```bash
curl http://127.0.0.1:8000/health
```

期望返回：

```json
{"status": "ok"}
```

API 文档：

- http://127.0.0.1:8000/docs
- http://127.0.0.1:8000/openapi.json

## 启动前端

另开一个终端：

```bash
uv run python -m streamlit run frontend/streamlit_app.py --server.address 127.0.0.1 --server.port 8501
```

浏览器访问：

- http://127.0.0.1:8501

前端默认调用 `http://localhost:8000/api/v1`。如需覆盖：

```bash
set API_BASE_URL=http://127.0.0.1:8000/api/v1
```

或在 Streamlit secrets 中配置：

```toml
api_url = "http://127.0.0.1:8000/api/v1"
```

## 体验推荐流程

后端和前端都启动后，在 Streamlit 页面输入：

```text
NLP方向导师推荐
```

也可以直接调用 API：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/recommend \
  -H "Content-Type: application/json" \
  -d '{"query":"NLP方向导师推荐","top_k":3,"filters":{},"options":{"enable_vector":true,"enable_graph":true}}'
```

正常情况下，`data/app.db` 已导入数据后会返回非空 `recommendations`。

## API 示例与响应结构

### 推荐接口

```bash
curl -X POST http://127.0.0.1:8000/api/v1/recommend \
  -H "Content-Type: application/json" \
  -d '{
    "query": "医学影像和计算机视觉方向导师推荐",
    "top_k": 3,
    "filters": {
      "org_unit": null,
      "title": null
    },
    "options": {
      "enable_vector": true,
      "enable_graph": true
    }
  }'
```

推荐结果核心结构如下。真实接口外层还会包含统一响应字段 `code`、`message`、`data` 和 `request_id`，其中 `data.request_id` 用于后续追问。

```json
{
  "query_understanding": {
    "research_interests": ["医学影像", "计算机视觉"],
    "preferred_locations": ["上海"],
    "preferred_universities": [],
    "degree_stage": "硕士",
    "constraints": [],
    "uncertainties": ["未明确是否偏理论或应用"]
  },
  "recommendations": [
    {
      "professor_id": "p_001",
      "name": "张三",
      "university": "某某大学",
      "college": "计算机学院",
      "title": "教授",
      "research_fields": ["医学影像", "深度学习", "计算机视觉"],
      "homepage_url": "https://example.edu.cn/professor/zhangsan",
      "match_level": "高",
      "match_score": 0.91,
      "reasons": [
        "研究方向包含医学影像和计算机视觉",
        "所在学校位于上海，符合地域偏好",
        "个人简介中包含深度学习相关内容"
      ],
      "limitations": [
        "公开资料中未明确招生信息"
      ]
    }
  ],
  "follow_up_questions": [
    "你更倾向于理论研究还是应用研究？",
    "是否只考虑 985 高校？"
  ]
}
```

当前实现的单条推荐还会返回 `item_id`、`summary`、`source_url`、`updated_at`、`evidence`、`graph_paths` 等字段；前端会在字段缺失时显示“暂无”而不是报错。

### 追问接口

把上一轮响应中的 `data.request_id` 或外层 `request_id` 填入 `previous_request_id`：

```bash
curl -X POST http://127.0.0.1:8000/api/v1/recommend/follow-up \
  -H "Content-Type: application/json" \
  -d '{
    "previous_request_id": "req_1700000000000_abcd1234",
    "query": "更希望导师在成都，偏应用研究",
    "top_k": 3,
    "filters": {},
    "options": {
      "enable_vector": true,
      "enable_graph": true
    }
  }'
```

追问成功时返回同样的推荐响应结构，并在 `data.changes` 中补充新增约束、排名变化和说明。

### 管理员修正导师数据

```bash
curl -X PATCH http://127.0.0.1:8000/api/v1/admin/items/1 \
  -H "Content-Type: application/json" \
  -d '{
    "operator_id": "admin-local",
    "reason": "修正公开主页和研究方向",
    "updates": {
      "research_areas": "医学影像、计算机视觉、深度学习",
      "metadata_json": "{\"homepage_url\":\"https://example.edu.cn/professor/zhangsan\",\"source_url\":\"https://example.edu.cn/faculty/zhangsan\",\"updated_at\":\"2026-01-02\"}"
    }
  }'
```

### 单条导师刷新状态

```bash
curl -X POST http://127.0.0.1:8000/api/v1/admin/items/1/refresh
```

该接口当前返回单条导师的热更新状态提示；向量和图谱仍需要按上文执行全量重建。

## 常用管理命令

导入已登记数据源：

```bash
doppler run --project yanclaw --config dev_personal -- uv run python -m app.jobs.import_dataset
```

一键重建导入、图谱和向量索引：

```bash
doppler run --project yanclaw --config dev_personal -- uv run python -m app.jobs.rebuild_all
```

重建图谱：

```bash
doppler run --project yanclaw --config dev_personal -- uv run python -m app.jobs.build_graph
```

重建向量索引：

```bash
doppler run --project yanclaw --config dev_personal -- uv run python -m app.jobs.build_vector_index
```

查看业务库数据量：

```bash
uv run python -c "import sqlite3; con=sqlite3.connect('data/app.db'); print('items', con.execute('select count(*) from items').fetchone()[0]); print('graph_edges', con.execute('select count(*) from graph_edges').fetchone()[0]); con.close()"
```

清理旧版导入遗留的无来源 `items`：

```bash
doppler run --project yanclaw --config dev_personal -- uv run python -m app.jobs.clean_legacy_items --dry-run
doppler run --project yanclaw --config dev_personal -- uv run python -m app.jobs.clean_legacy_items
```

正式清理会先备份 `data/app.db` 到 `data/backups/`，再删除 `source_id IS NULL` 的旧记录并重建图谱。

## 测试与验证

推荐先跑隔离夹具测试，默认不依赖真实 `data/app.db` 或 `data/chroma`：

```bash
uv run python -m pytest -m "not realdata" -q
```

如果只想快速验证稳定的排序和 LLM 降级逻辑，可执行：

```bash
uv run python -m pytest tests/test_ranking.py tests/test_llm.py -q
```

也可以跑稳定的单元测试和隔离夹具契约测试组合：

```bash
uv run python -m pytest tests/test_llm.py tests/test_ranking.py tests/test_contract_fixtures.py tests/test_models.py -q
```

当前已知测试注意事项：

- `tests/conftest.py` 提供 `temp_engine`、`db_session`、`temp_chroma_path`、`test_settings` 和 `isolated_app_state`，默认使用临时 SQLite/Chroma 路径，不读写 `data/app.db` 或 `data/chroma`。
- 隔离 fixture 测试用于验证 API/服务契约和数据模型，不要求本地已经导入真实 SCU 数据。
- `tests/test_models.py` 已使用临时 SQLite fixture，可重复运行且不会触碰真实业务库。
- `tests/test_api.py` 中除 `/health` 外的接口烟测标记为 `@pytest.mark.realdata`，它们依赖已准备好的 `data/app.db`/图谱/向量运行状态。
- 默认跳过真实数据测试可执行：`uv run python -m pytest -m "not realdata" -q`。
- 需要验证真实数据 API 时，先按上文导入数据并构建图谱/向量索引，再执行：`uv run python -m pytest -m realdata tests/test_api.py -q`。

## Docker 说明

Dockerfile 和 Compose 已存在，但本地调试建议优先使用 `uv + Doppler`。原因：Docker 构建会安装 Chroma、pandas、scikit-learn 等大依赖，首次构建耗时较长。

如需尝试容器构建：

```bash
docker build -t yanclaw-recommender-agent:local .
docker compose up --build
```

Compose 当前主要启动 FastAPI 后端和 nginx，不包含 Streamlit 前端。

## 排错

推荐结果为 0：

1. 确认原始 `.db` 在 `raw_data/`。
2. 确认 `configs/sources.yaml` 中对应数据源 `enabled: true`。
3. 确认 `data/app.db` 的 `items` 不为空。
4. 重新执行 `uv run python -m app.jobs.rebuild_all`。

Streamlit 报 `StreamlitSecretNotFoundError`：

- 当前代码已支持无 secrets 文件 fallback；如果仍报错，确认使用的是最新 `frontend/streamlit_app.py`。

端口被占用：

```bash
netstat -ano | findstr ":8000 :8501"
```

然后按 PID 结束对应进程。

LLM 调用失败：

- 确认 Doppler 中 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 正确。
- LLM 失败时推荐理由会走模板降级，但意图解析质量会下降。

## 协作者开发建议

- 不要提交真实 `.env` 或密钥。
- 改数据导入后，至少验证 `items` 和 `graph_edges` 计数。
- 改推荐逻辑后，至少验证 `POST /api/v1/recommend` 返回非空结果。
- 改前端后，打开 `http://127.0.0.1:8501` 手动提交一次查询。
- 提交前先说明是否改动了本地数据库文件，避免把个人运行状态误提交。

