from fastapi import APIRouter, Depends
from typing import Optional, List, Dict, Any
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.response import success_response
from app.services.recommendation_service import RecommendationService

settings = get_settings()
router = APIRouter()
rec_service = RecommendationService()


class RecommendRequest(BaseModel):
    user_id: Optional[int] = None
    query: str = ""
    scene: Optional[str] = "home"
    top_k: Optional[int] = None
    filters: Optional[Dict[str, Any]] = None
    options: Optional[Dict[str, Any]] = None


@router.post("/recommend")
async def recommend(request: RecommendRequest):
    """
    智能推荐接口
    基于用户输入、用户画像、历史行为进行混合推荐
    """
    result = await rec_service.recommend(
        query=request.query,
        user_id=request.user_id,
        top_k=request.top_k or settings.DEFAULT_TOP_K,
        filters=request.filters,
        options=request.options,
    )
    return result


@router.get("/recommend/home")
async def recommend_home(user_id: Optional[int] = None, top_k: int = 10):
    """首页推荐"""
    return await rec_service.recommend(
        query="",
        user_id=user_id,
        top_k=top_k,
    )


@router.get("/recommend/similar/{item_id}")
async def recommend_similar(item_id: int, top_k: int = 10):
    """相似推荐"""
    return await rec_service.recommend(
        query=f"similar:{item_id}",
        top_k=top_k,
    )
