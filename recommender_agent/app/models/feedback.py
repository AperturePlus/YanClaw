from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class Feedback(SQLModel, table=True):
    __tablename__ = "feedbacks"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    item_id: int = Field(foreign_key="items.id")
    request_id: Optional[str] = None
    feedback_type: str
    feedback_text: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
