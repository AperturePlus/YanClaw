from fastapi import APIRouter
from typing import Optional
from pydantic import BaseModel

from app.core.response import success_response
from app.models.feedback import Feedback
from app.models.base import engine
from sqlmodel import Session

router = APIRouter()


class FeedbackCreate(BaseModel):
    user_id: int
    item_id: int
    feedback_type: str  # like, dislike, not_interested
    feedback_text: Optional[str] = None


@router.post("/feedbacks")
async def create_feedback(feedback: FeedbackCreate):
    """提交反馈"""
    with Session(engine) as session:
        fb = Feedback(**feedback.model_dump())
        session.add(fb)
        session.commit()
        return success_response(data={"feedback_id": fb.id})


@router.get("/feedbacks/user/{user_id}")
async def get_user_feedbacks(user_id: int):
    """获取用户反馈列表"""
    with Session(engine) as session:
        feedbacks = session.query(Feedback).where(Feedback.user_id == user_id).all()
        return success_response(data=[
            {
                "id": f.id,
                "item_id": f.item_id,
                "feedback_type": f.feedback_type,
                "feedback_text": f.feedback_text,
                "created_at": str(f.created_at),
            }
            for f in feedbacks
        ])
