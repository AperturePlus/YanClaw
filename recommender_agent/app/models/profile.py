from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class UserProfile(SQLModel, table=True):
    __tablename__ = "user_profiles"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", unique=True)
    preferred_categories: Optional[str] = None
    preferred_tags: Optional[str] = None
    disliked_tags: Optional[str] = None
    profile_summary: Optional[str] = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)
