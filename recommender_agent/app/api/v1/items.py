from fastapi import APIRouter, Depends
from typing import Optional, Dict, Any
from pydantic import BaseModel

from app.core.response import success_response
from app.services.item_service import ItemService

router = APIRouter()
item_service = ItemService()


class ItemCreate(BaseModel):
    title: str
    category: Optional[str] = None
    description: Optional[str] = None
    research_areas: Optional[str] = None
    org_unit: Optional[str] = None
    tags: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@router.get("/items/search")
async def search_items(
    keyword: Optional[str] = None,
    category: Optional[str] = None,
    org_unit: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    """搜索导师/物品"""
    result = ItemService.list_items(
        keyword=keyword,
        category=category,
        org_unit=org_unit,
        page=page,
        page_size=page_size,
    )
    return success_response(data=result)


@router.get("/items/{item_id}")
async def get_item(item_id: int):
    """获取导师详情"""
    item = ItemService.get_item(item_id)
    if not item:
        return success_response(data=None, message="物品不存在")
    return success_response(data={
        "id": item.id,
        "title": item.title,
        "category": item.category,
        "org_unit": item.org_unit,
        "research_areas": item.research_areas,
        "description": item.description,
        "tags": item.tags,
    })


@router.post("/items")
async def create_item(item: ItemCreate):
    """创建导师/物品"""
    new_item = ItemService.create_item(item.model_dump())
    return success_response(data={"id": new_item.id, "title": new_item.title})
