from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class GraphEdge(SQLModel, table=True):
    __tablename__ = "graph_edges"
    id: Optional[int] = Field(default=None, primary_key=True)
    source_id: str
    source_type: str
    target_id: str
    target_type: str
    relation: str
    weight: float = 1.0
    confidence: float = 1.0
    evidence: Optional[str] = None
    data_source_id: Optional[str] = None
    batch_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
