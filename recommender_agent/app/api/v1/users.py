from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from app.core.response import success_response
from app.models.user import User
from app.models.profile import UserProfile
from app.models.base import engine
from sqlmodel import Session

router = APIRouter()


class UserCreate(BaseModel):
    username: str
    research_interests: Optional[str] = None
    preferred_tags: Optional[str] = None


@router.post("/users")
async def create_user(user: UserCreate):
    """创建用户"""
    with Session(engine) as session:
        db_user = User(**user.model_dump())
        session.add(db_user)
        session.commit()
        session.refresh(db_user)
        return success_response(data={"user_id": db_user.id, "username": db_user.username})


@router.get("/users/{user_id}")
async def get_user(user_id: int):
    """获取用户信息"""
    with Session(engine) as session:
        user = session.query(User).where(User.id == user_id).first()
        if not user:
            return success_response(data=None, message="用户不存在")
        return success_response(data={
            "id": user.id,
            "username": user.username,
            "research_interests": user.research_interests,
            "preferred_tags": user.preferred_tags,
        })


@router.get("/users/{user_id}/profile")
async def get_user_profile(user_id: int):
    """获取用户画像"""
    with Session(engine) as session:
        profile = session.query(UserProfile).where(UserProfile.user_id == user_id).first()
        if not profile:
            return success_response(data=None, message="用户画像不存在")
        return success_response(data={
            "user_id": profile.user_id,
            "preferred_categories": profile.preferred_categories,
            "preferred_tags": profile.preferred_tags,
            "disliked_tags": profile.disliked_tags,
            "profile_summary": profile.profile_summary,
        })
