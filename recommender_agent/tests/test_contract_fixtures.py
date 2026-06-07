from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlmodel import Session

from app.core import config
from app.core.config import Settings
from app.models.item import Item


def test_temp_engine_creates_contract_tables(temp_engine: Engine) -> None:
    inspector = inspect(temp_engine)

    assert "items" in inspector.get_table_names()
    assert "org_units" in inspector.get_table_names()
    assert "recommendation_logs" in inspector.get_table_names()
    assert "feedbacks" in inspector.get_table_names()


def test_db_session_uses_temp_sqlite(db_session: Session) -> None:
    item = Item(title="Fixture Mentor", category="Fixture College")

    db_session.add(item)
    db_session.commit()

    assert item.id is not None


def test_temp_chroma_path_is_isolated(temp_chroma_path: Path) -> None:
    assert temp_chroma_path.exists()
    assert temp_chroma_path.name == "chroma"
    assert "data" not in temp_chroma_path.parts


def test_settings_override_uses_temp_paths(test_settings: Settings) -> None:
    assert config.get_settings() is test_settings
    assert test_settings.DATABASE_URL.startswith("sqlite:///")
    assert "data/app.db" not in test_settings.DATABASE_URL.replace("\\", "/")
    assert Path(test_settings.CHROMA_PATH).name == "chroma"
    assert "data" not in Path(test_settings.CHROMA_PATH).parts
