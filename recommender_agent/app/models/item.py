from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class Item(SQLModel, table=True):
    __tablename__ = "items"
    id: Optional[int] = Field(default=None, primary_key=True)
    external_id: Optional[int] = None
    title: str
    category: Optional[str] = None
    description: Optional[str] = None
    research_areas: Optional[str] = None
    org_unit: Optional[str] = None
    tags: Optional[str] = None
    rating: Optional[float] = None
    popularity: Optional[float] = None
    metadata_json: Optional[str] = None
    vector_id: Optional[str] = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)
