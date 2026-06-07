from collections.abc import Iterator
from importlib import import_module
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import SQLModel, Session, create_engine

from app.core.config import Settings, get_settings
from app.models import base as model_base


@pytest.fixture
def temp_workspace() -> Iterator[Path]:
    """Create a per-test temporary workspace inside recommender_agent/.pytest_tmp."""
    temp_root = Path.cwd() / ".pytest_tmp"
    temp_root.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=temp_root) as temp_dir:
        yield Path(temp_dir)


@pytest.fixture
def temp_chroma_path(temp_workspace: Path) -> Path:
    """Return an isolated Chroma directory for tests that need vector storage."""
    chroma_path = temp_workspace / "chroma"
    chroma_path.mkdir()
    return chroma_path


@pytest.fixture
def temp_sqlite_url(temp_workspace: Path) -> str:
    """Return a SQLite URL pointing at a per-test temporary database file."""
    return f"sqlite:///{temp_workspace / 'test_app.db'}"


@pytest.fixture
def temp_engine(temp_sqlite_url: str) -> Iterator[Engine]:
    """Create a temporary SQLite engine and initialize all SQLModel tables."""
    _ = import_module("app.models")
    engine = create_engine(
        temp_sqlite_url,
        connect_args={"check_same_thread": False},
    )
    SQLModel.metadata.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(temp_engine: Engine) -> Iterator[Session]:
    """Provide a SQLModel session bound to the isolated test engine."""
    with Session(temp_engine) as session:
        yield session


@pytest.fixture
def test_settings(
    monkeypatch: pytest.MonkeyPatch,
    temp_sqlite_url: str,
    temp_chroma_path: Path,
) -> Iterator[Settings]:
    """Override cached application settings with test-safe paths."""
    get_settings.cache_clear()
    settings = Settings(
        DATABASE_URL=temp_sqlite_url,
        CHROMA_PATH=str(temp_chroma_path),
        ENABLE_LLM=False,
        ENABLE_VECTOR=False,
        ENABLE_GRAPH=False,
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr(model_base, "settings", settings)
    try:
        yield settings
    finally:
        get_settings.cache_clear()


@pytest.fixture
def isolated_app_state(
    monkeypatch: pytest.MonkeyPatch,
    temp_engine: Engine,
    test_settings: Settings,
) -> Settings:
    """Patch module-level app engine/settings references to isolated test state."""
    monkeypatch.setattr(model_base, "engine", temp_engine)

    engine_import_paths = [
        "app.core.dependencies.engine",
        "app.api.v1.feedback.engine",
        "app.api.v1.users.engine",
        "app.jobs.build_graph.engine",
        "app.jobs.build_vector_index.engine",
        "app.jobs.import_dataset.target_engine",
        "app.jobs.rebuild_all.engine",
        "app.services.graph_service.engine",
        "app.services.item_service.engine",
        "app.services.recommendation_service.engine",
        "app.services.retrieval_service.engine",
    ]
    for import_path in engine_import_paths:
        module_name, attribute_name = import_path.rsplit(".", maxsplit=1)
        module = sys.modules.get(module_name)
        if module is not None:
            monkeypatch.setattr(module, attribute_name, temp_engine, raising=False)

    settings_import_paths = [
        "app.api.v1.recommend.settings",
        "app.jobs.build_graph.settings",
        "app.jobs.build_vector_index.settings",
        "app.jobs.import_dataset.settings",
        "app.jobs.rebuild_all.settings",
        "app.main.settings",
        "app.services.explanation_service.settings",
        "app.services.graph_service.settings",
        "app.services.item_service.settings",
        "app.services.llm_service.settings",
        "app.services.ranking_service.settings",
        "app.services.recommendation_service.settings",
        "app.services.retrieval_service.settings",
        "app.services.vector_service.settings",
    ]
    for import_path in settings_import_paths:
        module_name, attribute_name = import_path.rsplit(".", maxsplit=1)
        module = sys.modules.get(module_name)
        if module is not None:
            monkeypatch.setattr(module, attribute_name, test_settings, raising=False)

    return test_settings


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "realdata: test requires prepared local data/app.db or other runtime data",
    )
