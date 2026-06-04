from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel
from typing import Optional

from app.core.response import success_response

router = APIRouter()


class RebuildVectorRequest(BaseModel):
    category: Optional[str] = None


class RebuildGraphRequest(BaseModel):
    force: bool = False


@router.post("/admin/vector/rebuild")
async def rebuild_vector_index(request: RebuildVectorRequest):
    """重建向量索引"""
    from app.jobs.build_vector_index import build_vector_index
    build_vector_index()
    return success_response(data={"message": "向量索引重建完成"})


@router.post("/admin/graph/reload")
async def reload_graph(request: RebuildGraphRequest):
    """重建知识图谱"""
    from app.jobs.build_graph import build_knowledge_graph, save_graph_edges
    G = build_knowledge_graph()
    save_graph_edges(G)
    return success_response(data={
        "node_count": G.number_of_nodes(),
        "edge_count": G.number_of_edges(),
    })


@router.post("/admin/import")
async def import_data():
    """导入数据源数据并重建推荐索引。"""
    from app.jobs.build_graph import build_knowledge_graph, save_graph_edges
    from app.jobs.build_vector_index import build_vector_index
    from app.jobs.import_dataset import import_scu_data

    import_result = import_scu_data()
    graph = build_knowledge_graph()
    save_graph_edges(graph)
    build_vector_index()

    return success_response(data={
        "message": "数据导入完成",
        "import": import_result,
        "graph": {
            "node_count": graph.number_of_nodes(),
            "edge_count": graph.number_of_edges(),
        },
    })
