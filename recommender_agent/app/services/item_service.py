from typing import List, Dict, Any, Optional

from app.core.config import get_settings
from app.models.item import Item
from sqlmodel import Session, select
from app.models.base import engine

settings = get_settings()


class ItemService:
    """Service for item CRUD operations"""

    @staticmethod
    def get_item(item_id: int) -> Optional[Item]:
        """Get item by ID"""
        with Session(engine) as session:
            return session.exec(select(Item).where(Item.id == item_id)).first()

    @staticmethod
    def list_items(
        category: Optional[str] = None,
        org_unit: Optional[str] = None,
        tag: Optional[str] = None,
        keyword: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """List items with optional filters"""
        with Session(engine) as session:
            query = select(Item).where(Item.is_active == True)

            if category:
                query = query.where(Item.category == category)
            if org_unit:
                query = query.where(Item.org_unit == org_unit)
            if keyword:
                query = query.where(
                    (Item.title.contains(keyword)) |
                    (Item.description.contains(keyword)) |
                    (Item.research_areas.contains(keyword))
                )

            # Count total
            total = len(session.exec(query).all())

            # Paginate
            offset = (page - 1) * page_size
            items = session.exec(query.offset(offset).limit(page_size)).all()

            return {
                "total": total,
                "page": page,
                "page_size": page_size,
                "items": [
                    {
                        "id": item.id,
                        "title": item.title,
                        "category": item.category,
                        "org_unit": item.org_unit,
                        "research_areas": item.research_areas,
                        "description": item.description,
                    }
                    for item in items
                ],
            }

    @staticmethod
    def create_item(item_data: Dict[str, Any]) -> Item:
        """Create a new item"""
        with Session(engine) as session:
            item = Item(**item_data)
            session.add(item)
            session.commit()
            session.refresh(item)
            return item
