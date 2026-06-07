from typing import List
from datetime import datetime
import hashlib

import chromadb
from chromadb.config import Settings as ChromaSettings
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.logging import get_logger
from app.models.base import engine
from app.models import Item

settings = get_settings()
logger = get_logger()


def build_vector_text(item: Item) -> str:
    """Construct text for vector embedding from item data"""
    parts = []
    if item.title:
        parts.append(f"姓名: {item.title}")
    if item.source_id:
        parts.append(f"数据源: {item.source_id}")
    if item.research_areas:
        parts.append(f"研究方向: {item.research_areas}")
    if item.description:
        parts.append(f"简介: {item.description}")
    if item.org_unit:
        parts.append(f"学院: {item.org_unit}")
    if item.tags:
        parts.append(f"标签: {item.tags}")
    return "\n".join(parts)


def build_vector_index():
    """Build ChromaDB vector index for all items"""
    client = chromadb.PersistentClient(
        path=settings.CHROMA_PATH,
        settings=ChromaSettings(anonymized_telemetry=False),
    )

    # Get or create collection
    collection = client.get_or_create_collection(
        name=settings.CHROMA_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )

    with Session(engine) as session:
        items = session.exec(select(Item).where(Item.is_active == True)).all()

        ids = []
        documents = []
        metadatas = []

        for item in items:
            text = build_vector_text(item)
            text_hash = hashlib.sha256(text.encode()).hexdigest()

            # Skip if vector_id matches current hash
            if item.vector_id == text_hash:
                continue

            ids.append(str(item.id))
            documents.append(text)
            metadatas.append({
                "item_id": item.id,
                "entity_id": item.entity_id or "",
                "entity_type": item.source_entity_type or "professor",
                "source_id": item.source_id or "",
                "source_external_id": item.source_external_id or item.external_id or "",
                "title": item.title,
                "category": item.category or "",
                "org_unit": item.org_unit or "",
            })

            item.vector_id = text_hash

        if ids:
            collection.add(
                ids=ids,
                documents=documents,
                metadatas=metadatas,
            )
            session.commit()
            logger.info("Indexed %s items into ChromaDB", len(ids))
        else:
            logger.info("No items to index (all up to date)")


if __name__ == "__main__":
    build_vector_index()
