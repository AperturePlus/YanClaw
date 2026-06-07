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

    def test_rank_candidates_includes_domain_and_rerank_weights(self):
        candidates = [
            {
                "item_id": 1,
                "semantic_score": 0.0,
                "graph_score": 0.0,
                "profile_score": 0.0,
                "popularity_score": 0.0,
                "domain_score": 1.0,
                "rerank_score": 1.0,
            },
            {
                "item_id": 2,
                "semantic_score": 0.9,
                "graph_score": 0.0,
                "profile_score": 0.0,
                "popularity_score": 0.0,
            },
        ]
        service = RankingService()

        ranked = service.rank_candidates(candidates, top_k=2)

        assert ranked[0]["item_id"] == 1
        assert ranked[0]["final_score"] == 0.4
        assert ranked[1]["final_score"] == 0.365

    def test_apply_rerank_scores_merges_evidence_and_truncates(self):
        shortlist = [
            {"item_id": 1, "semantic_score": 0.5, "graph_score": 0.0, "profile_score": 0.0, "popularity_score": 0.0},
            {"item_id": 2, "semantic_score": 0.4, "graph_score": 0.0, "profile_score": 0.0, "popularity_score": 0.0},
        ]
        rerank_results = [
            {"item_id": 2, "rerank_score": 1.0, "rerank_evidence": {"provider": "heuristic"}},
            {"item_id": 1, "rerank_score": 0.0, "rerank_evidence": {"provider": "heuristic"}},
        ]
        service = RankingService()

        ranked = service.apply_rerank_scores(shortlist, rerank_results, top_k=1)

        assert [candidate["item_id"] for candidate in ranked] == [2]
        assert ranked[0]["rerank_score"] == 1.0
        assert ranked[0]["rerank_evidence"] == {"provider": "heuristic"}
