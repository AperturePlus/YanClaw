from typing import Generator

from fastapi import Depends
from sqlmodel import Session

from app.core.config import get_settings, Settings
from app.models.base import engine


def get_settings_dep() -> Settings:
    return get_settings()


def get_db() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session


SettingsDep = Depends(get_settings_dep)
DbSession = Depends(get_db)
