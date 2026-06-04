from typing import List, Dict, Any
from app.core.config import get_settings

settings = get_settings()


class RankingService:
    """Service for hybrid ranking and scoring"""

    def rank_candidates(
        self, candidates: List[Dict[str, Any]], top_k: int = 10
    ) -> List[Dict[str, Any]]:
        """Calculate final score and rank candidates"""
        for candidate in candidates:
            semantic = candidate.get("semantic_score", 0)
            graph = candidate.get("graph_score", 0)
            profile = candidate.get("profile_score", 0)
            popularity = candidate.get("popularity_score", 0)
            freshness = 0.5  # Default freshness

            # Weighted sum
            final = (
                settings.SEMANTIC_WEIGHT * semantic +
                settings.GRAPH_WEIGHT * graph +
                settings.PROFILE_WEIGHT * profile +
                settings.POPULARITY_WEIGHT * popularity +
                settings.FRESHNESS_WEIGHT * freshness
            )
            candidate["final_score"] = round(final, 4)

        # Sort by final score descending
        candidates.sort(key=lambda x: x["final_score"], reverse=True)

        # Deduplicate by item_id
        seen_ids = set()
        unique_candidates = []
        for c in candidates:
            if c["item_id"] not in seen_ids:
                seen_ids.add(c["item_id"])
                unique_candidates.append(c)

        return unique_candidates[:top_k]
