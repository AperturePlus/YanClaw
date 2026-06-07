from sqlalchemy import inspect, text
from sqlmodel import SQLModel, create_engine

from app.core.config import get_settings

settings = get_settings()
engine = create_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {}
)


def init_db():
    SQLModel.metadata.create_all(engine)
    ensure_runtime_schema()


def ensure_runtime_schema():
    if "sqlite" not in settings.DATABASE_URL:
        return

    item_columns = {
        "entity_id": "VARCHAR",
        "source_id": "VARCHAR",
        "source_entity_type": "VARCHAR",
        "source_pk": "VARCHAR",
        "source_external_id": "VARCHAR",
        "content_hash": "VARCHAR",
        "quality_score": "FLOAT",
    }

    graph_edge_columns = {
        "data_source_id": "VARCHAR",
        "batch_id": "VARCHAR",
        "confidence": "FLOAT DEFAULT 1.0",
        "updated_at": "DATETIME",
    }

    with engine.begin() as connection:
        inspector = inspect(connection)
        tables = set(inspector.get_table_names())
        if "items" in tables:
            existing = {column["name"] for column in inspector.get_columns("items")}
            for column_name, column_type in item_columns.items():
                if column_name not in existing:
                    connection.execute(text(f"ALTER TABLE items ADD COLUMN {column_name} {column_type}"))
        if "graph_edges" in tables:
            existing = {column["name"] for column in inspector.get_columns("graph_edges")}
            for column_name, column_type in graph_edge_columns.items():
                if column_name not in existing:
                    connection.execute(text(f"ALTER TABLE graph_edges ADD COLUMN {column_name} {column_type}"))
