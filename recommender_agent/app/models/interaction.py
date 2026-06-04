from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class Interaction(SQLModel, table=True):
    __tablename__ = "interactions"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    item_id: int = Field(foreign_key="items.id")
    action_type: str
    rating: Optional[float] = None
    weight: float = 1.0
    context_json: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
