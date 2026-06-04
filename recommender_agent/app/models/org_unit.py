from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class OrgUnit(SQLModel, table=True):
    __tablename__ = "org_units"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(unique=True)
    url: str
    kind: Optional[str] = None
    status: str = "active"
    created_at: datetime = Field(default_factory=datetime.utcnow)
