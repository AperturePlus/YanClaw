import pytest

from app.services.ranking_service import RankingService


class TestRankingService:
    def test_rank_candidates(self):
        candidates = [
            {"item_id": 1, "semantic_score": 0.9, "graph_score": 0.8, "profile_score": 0.7, "sources": ["vector"]},
            {"item_id": 2, "semantic_score": 0.5, "graph_score": 0.9, "profile_score": 0.6, "sources": ["graph"]},
            {"item_id": 3, "semantic_score": 0.3, "graph_score": 0.3, "profile_score": 0.3, "sources": ["structured"]},
        ]
        service = RankingService()
        ranked = service.rank_candidates(candidates, top_k=2)
        assert len(ranked) <= 2
        assert ranked[0]["final_score"] >= ranked[1]["final_score"]

    def test_empty_candidates(self):
        service = RankingService()
        ranked = service.rank_candidates([], top_k=10)
        assert ranked == []
