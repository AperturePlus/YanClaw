import streamlit as st
import requests
import json
import pandas as pd
import os

def get_api_base():
    env_api_url = os.environ.get("API_BASE_URL")
    if env_api_url:
        return env_api_url
    try:
        return st.secrets.get("api_url", "http://localhost:8000/api/v1")
    except st.errors.StreamlitSecretNotFoundError:
        return "http://localhost:8000/api/v1"


API_BASE = get_api_base()


def dot_escape(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


st.set_page_config(
    page_title="AI导师推荐系统",
    page_icon="🎓",
    layout="wide",
)

st.title("🎓 AI 导师推荐系统")
st.subheader("基于知识图谱的智能导师匹配")

# Sidebar
st.sidebar.header("搜索设置")
with st.sidebar.form("search_form"):
    query = st.text_input("输入您的研究方向或需求", placeholder="例如：NLP方向的自然语言处理导师")
    top_k = st.slider("推荐数量", 1, 20, 10)
    org_filter = st.text_input("学院筛选（可选）")
    title_filter = st.text_input("职称筛选（可选）")
    enable_vector = st.checkbox("语义检索", value=True)
    enable_graph = st.checkbox("图谱推荐", value=True)
    submitted = st.form_submit_button("开始推荐")

# Main content
if submitted and query:
    with st.spinner("正在生成推荐..."):
        try:
            resp = requests.post(
                f"{API_BASE}/recommend",
                json={
                    "query": query,
                    "top_k": top_k,
                    "filters": {
                        "org_unit": org_filter if org_filter else None,
                        "title": title_filter if title_filter else None,
                    },
                    "options": {
                        "enable_vector": enable_vector,
                        "enable_graph": enable_graph,
                    },
                },
                timeout=90,
            )
            result = resp.json()

            if result.get("code") == 0:
                data = result.get("data", {})
                recommendations = data.get("recommendations", [])
                recommendations.sort(key=lambda rec: rec.get("score", 0), reverse=True)
                latency = data.get("latency_ms", 0)

                st.success(f"推荐完成！耗时 {latency}ms")

                # Query understanding
                q_understanding = data.get("query_understanding", {})
                if q_understanding:
                    st.info(
                        f"**意图**: {q_understanding.get('intent', '')}  |  "
                        f"**关键词**: {', '.join(q_understanding.get('keywords', []))}"
                    )

                # Results
                st.header(f"推荐结果（{len(recommendations)}位导师）")

                for i, rec in enumerate(recommendations):
                    with st.container():
                        col1, col2 = st.columns([3, 1])
                        with col1:
                            st.subheader(f"{i+1}. {rec['title']}")
                            if rec.get('org_unit'):
                                st.caption(f"📍 {rec['org_unit']}")
                            if rec.get('tags'):
                                st.caption(f"职称：{rec['tags']}")
                            if rec.get('research_areas'):
                                st.write(f"🔬 研究方向：{rec['research_areas']}")
                            if rec.get('description'):
                                st.write(f"📝 {rec['description'][:200]}...")
                            if rec.get('reason'):
                                st.info(f"💡 {rec['reason']}")

                        with col2:
                            score = rec.get('score', 0)
                            st.metric("匹配度", f"{score:.2%}")
                            evidence = rec.get('evidence', {})
                            if evidence:
                                st.bar_chart(
                                    pd.DataFrame({
                                        "维度": list(evidence.keys()),
                                        "分数": list(evidence.values()),
                                    }).set_index("维度")
                                )
                            st.write(f"来源: {', '.join(rec.get('sources', []))}")

                        st.divider()

                # Graph visualization
                st.header("知识图谱可视化")
                graph_data = data.get("graph_paths", [])
                if graph_data:
                    node_ids = {}
                    dot_lines = [
                        "graph RecommendationGraph {",
                        "  rankdir=LR;",
                        '  graph [bgcolor="transparent"];',
                        '  node [shape=box, style="rounded,filled", fontname="Microsoft YaHei"];',
                        '  edge [fontname="Microsoft YaHei", color="#64748b"];',
                    ]

                    def add_node(node_key, label, fillcolor):
                        if node_key in node_ids:
                            return node_ids[node_key]
                        node_id = f"n{len(node_ids)}"
                        node_ids[node_key] = node_id
                        dot_lines.append(
                            f'  {node_id} [label="{dot_escape(label)}", fillcolor="{fillcolor}"];'
                        )
                        return node_id

                    for edge in graph_data:
                        source = edge.get("source")
                        target = edge.get("target")
                        if not source or not target:
                            continue

                        source_id = add_node(
                            source,
                            edge.get("source_label", source),
                            "#bfdbfe",
                        )
                        target_id = add_node(
                            target,
                            edge.get("target_label", target),
                            "#fde68a",
                        )
                        relation = dot_escape(edge.get("relation", "related"))
                        dot_lines.append(f'  {source_id} -- {target_id} [label="{relation}"];')

                    dot_lines.append("}")
                    st.graphviz_chart("\n".join(dot_lines))
                    with st.expander("查看图谱路径数据"):
                        st.json(graph_data)
                else:
                    st.info("暂无图谱路径信息")

            else:
                st.error(f"推荐失败: {result.get('message', '未知错误')}")

        except requests.exceptions.ConnectionError:
            st.error("无法连接到API服务，请确认后端服务已启动（端口8000）")
        except Exception as e:
            st.error(f"发生错误: {str(e)}")

# Footer
st.sidebar.markdown("---")
st.sidebar.info("LightGraphRec v0.1.0\n基于 FastAPI + NetworkX + ChromaDB")

