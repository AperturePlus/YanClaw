from sqlmodel import SQLModel, Field
from typing import Optional


class Tag(SQLModel, table=True):
    __tablename__ = "tags"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    type: Optional[str] = None
    description: Optional[str] = None


class ItemTag(SQLModel, table=True):
    __tablename__ = "item_tags"
    id: Optional[int] = Field(default=None, primary_key=True)
    item_id: int = Field(foreign_key="items.id")
    tag_id: int = Field(foreign_key="tags.id")
    weight: float = 1.0
