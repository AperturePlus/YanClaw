from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class RecommendationLog(SQLModel, table=True):
    __tablename__ = "recommendation_logs"
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: str = Field(unique=True)
    user_id: Optional[int] = None
    query: Optional[str] = None
    strategy: Optional[str] = None
    candidate_count: Optional[int] = None
    result_item_ids: Optional[str] = None
    latency_ms: Optional[int] = None
    debug_json: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
