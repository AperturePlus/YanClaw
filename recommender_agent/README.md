# LightGraphRec AI 导师推荐系统

LightGraphRec 是 `recommender_agent` 的导师推荐 MVP。系统从 `scu.edu.cn.db` 导入四川大学教师数据，写入业务库 `data/app.db`，再基于 SQLite、ChromaDB、NetworkX 和 LLM 组合完成导师召回、排序与推荐理由生成。

当前版本已经覆盖 `项目规划.md` 的核心演示路径：数据导入、FastAPI 后端、Streamlit 前端、结构化召回、向量索引、知识图谱边构建、推荐接口和基础测试。它还不是规划文档里的完整终态：Repository 层、Engine 层、完整图谱路径解释、CI/CD 和隔离测试夹具仍属于后续完善项。

## 当前可用能力

- 后端 API：FastAPI，入口 `app.main:app`，默认端口 `8000`。
- 前端 Demo：Streamlit，入口 `frontend/streamlit_app.py`，默认端口 `8501`。
- 数据源：`scu.edu.cn.db` 原始教师库。
- 业务库：`data/app.db`，包含 `items`、`org_units`、`graph_edges` 等推荐运行表。
- 数据准备：`app.jobs.import_dataset.import_scu_data()` 导入 SCU 教师为 `Item`。
- 图谱构建：`app.jobs.build_graph` 从 `items` 构建 `graph_edges`。
- 向量索引：`app.jobs.build_vector_index` 将 `items` 写入 `data/chroma`。
- 推荐接口：`POST /api/v1/recommend` 返回导师列表、分数、来源和推荐理由。
- 管理接口：`POST /api/v1/admin/import` 会导入数据并重建图谱/向量索引。

## 与项目规划的对齐情况

| 模块 | 状态 | 说明 |
|---|---|---|
| FastAPI 基础框架 | 已实现 | `/health`、统一响应、API 路由可用。 |
| 数据导入 | 已实现 | `scu.edu.cn.db -> data/app.db`，教师映射为 `items`。 |
| 结构化召回 | 已实现 | 无过滤时可从活跃 `items` 返回候选。 |
| ChromaDB 向量索引 | 已实现 | 可构建 `professors_vector` collection。 |
| NetworkX 图谱 | 部分实现 | 已构建 `Item -> OrgUnit` 边；复杂研究方向/合作路径仍较简单。 |
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
- `scu.edu.cn.db` 放在项目根目录

配置来源：

- 示例配置见 `.env.example`
- 当前真实配置建议使用 Doppler：project `yanclaw`，config `dev_personal`

关键环境变量：

```env
APP_NAME=LightGraphRec
APP_ENV=local
DATABASE_URL=sqlite:///data/app.db
SCU_SOURCE_DB=scu.edu.cn.db
CHROMA_PATH=data/chroma
CHROMA_COLLECTION=professors_vector
LLM_PROVIDER=deepseek
LLM_API_KEY=...
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
ENABLE_LLM=true
ENABLE_VECTOR=true
ENABLE_GRAPH=true
```

## 安装依赖

在 `recommender_agent` 目录执行：

```bash
uv pip install -r requirements.txt
uv pip install -e ".[dev]"
```

如果你不需要运行测试，可以只安装 `requirements.txt`。

## 准备推荐数据

首次运行前必须把 `scu.edu.cn.db` 导入业务库，并构建图谱/向量索引。

推荐一条命令完成导入和图谱构建：

```bash
doppler run --project yanclaw --config dev_personal -- uv run python -c "from app.jobs.import_dataset import import_scu_data; from app.jobs.build_graph import build_knowledge_graph, save_graph_edges; r=import_scu_data(); print(r); g=build_knowledge_graph(); print(g.number_of_nodes(), g.number_of_edges()); save_graph_edges(g)"
```

然后构建 ChromaDB 向量索引：

```bash
doppler run --project yanclaw --config dev_personal -- uv run python -m app.jobs.build_vector_index
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

## 常用管理命令

导入 SCU 数据：

```bash
doppler run --project yanclaw --config dev_personal -- uv run python -m app.jobs.import_dataset
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

## 测试与验证

推荐先跑稳定的单元测试：

```bash
doppler run --project yanclaw --config dev_personal -- uv run python -m pytest tests/test_llm.py tests/test_ranking.py -q
```

当前已知测试注意事项：

- `tests/test_models.py` 使用固定唯一值，并连接真实 `data/app.db`，重复运行或已导入数据后可能触发唯一约束冲突。后续应改为独立临时 SQLite fixture。
- `tests/test_api.py` 部分用例依赖 FastAPI lifespan 和已准备好的数据库数据，直接运行时可能受本地状态影响。

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

1. 确认 `scu.edu.cn.db` 在项目根目录。
2. 确认 `data/app.db` 的 `items` 不为空。
3. 重新执行数据导入、图谱构建和向量索引构建。

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

