from fastapi import APIRouter
from typing import Optional

from app.core.response import success_response
from app.services.graph_service import GraphService

router = APIRouter()
graph_service = GraphService()


@router.get("/graph/user/{user_id}")
async def get_user_graph(
    user_id: int,
    depth: int = 2,
    limit: int = 50,
):
    """获取用户关联图谱"""
    # Simplified implementation
    return success_response(data={
        "user_id": user_id,
        "nodes": [],
        "edges": [],
    })


@router.get("/graph/path")
async def get_graph_path(
    source: str,
    target: str,
    max_depth: int = 3,
):
    """获取两个节点之间的路径"""
    paths = graph_service.find_paths(source, target, max_depth)
    return success_response(data={"paths": paths})


@router.get("/graph/neighbors/{node_id}")
async def get_neighbors(
    node_id: str,
    relation: Optional[str] = None,
    limit: int = 20,
):
    """获取节点邻居"""
    neighbors = graph_service.get_neighbors(node_id, relation, limit)
    return success_response(data={"neighbors": neighbors})
