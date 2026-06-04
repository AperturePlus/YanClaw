import chromadb
from chromadb.config import Settings as ChromaSettings
from typing import List, Optional, Dict, Any

from app.core.config import get_settings
from app.models.item import Item

settings = get_settings()


class VectorService:
    """Service for ChromaDB vector operations"""

    def __init__(self):
        self.client = chromadb.PersistentClient(
            path=settings.CHROMA_PATH,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        self.collection = self.client.get_or_create_collection(
            name=settings.CHROMA_COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    def search_similar(self, query: str, top_k: int = 10) -> List[Dict[str, Any]]:
        """Semantic search for items similar to query text"""
        if not settings.ENABLE_VECTOR:
            return []

        try:
            results = self.collection.query(
                query_texts=[query],
                n_results=top_k,
                include=["metadatas", "distances"],
            )

            candidates = []
            for i in range(len(results["ids"][0])):
                meta = results["metadatas"][0][i]
                distance = results["distances"][0][i]
                candidates.append({
                    "item_id": int(meta["item_id"]),
                    "score": 1.0 - distance,  # cosine distance -> similarity
                    "source": "vector",
                })
            return candidates
        except Exception as e:
            print(f"Vector search error: {e}")
            return []

    def delete_item_vectors(self, item_ids: List[int]):
        """Remove vectors for given item IDs"""
        str_ids = [str(i) for i in item_ids]
        self.collection.delete(ids=str_ids)
