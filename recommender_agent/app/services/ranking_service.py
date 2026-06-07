from typing import Any

from app.core.config import get_settings

settings = get_settings()


class RankingService:
    """Service for hybrid ranking and scoring"""

    def rank_candidates(
        self, candidates: list[dict[str, Any]], top_k: int = 10
    ) -> list[dict[str, Any]]:
        """Calculate final score and rank candidates"""
        for candidate in candidates:
            semantic = candidate.get("semantic_score", 0.0)
            graph = candidate.get("graph_score", 0.0)
            profile = candidate.get("profile_score", 0.0)
            popularity = candidate.get("popularity_score", 0.0)
            domain = candidate.get("domain_score", 0.0)
            rerank = candidate.get("rerank_score", 0.0)
            freshness = 0.5  # Default freshness

            # Weighted sum
            final = (
                settings.SEMANTIC_WEIGHT * semantic +
                settings.GRAPH_WEIGHT * graph +
                settings.PROFILE_WEIGHT * profile +
                settings.POPULARITY_WEIGHT * popularity +
                settings.FRESHNESS_WEIGHT * freshness +
                settings.DOMAIN_WEIGHT * domain +
                settings.RERANK_WEIGHT * rerank
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

    def apply_rerank_scores(
        self,
        shortlist: list[dict[str, Any]],
        rerank_results: list[dict[str, Any]],
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """Merge reranker scores/evidence into a shortlist and recompute ranking."""
        rerank_by_item_id = {
            result.get("item_id"): result
            for result in rerank_results
            if result.get("item_id") is not None
        }
        enriched_shortlist: list[dict[str, Any]] = []
        for candidate in shortlist:
            updated = dict(candidate)
            rerank_result = rerank_by_item_id.get(candidate.get("item_id"))
            if rerank_result:
                updated["rerank_score"] = rerank_result.get("rerank_score", 0.0)
                if "rerank_evidence" in rerank_result:
                    updated["rerank_evidence"] = rerank_result["rerank_evidence"]
            else:
                updated.setdefault("rerank_score", 0.0)
            enriched_shortlist.append(updated)

        return self.rank_candidates(enriched_shortlist, top_k=top_k)
