from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class User(SQLModel, table=True):
    __tablename__ = "users"
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    research_interests: Optional[str] = None
    preferred_tags: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
