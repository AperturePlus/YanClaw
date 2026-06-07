from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

settings = get_settings()
logger = get_logger()


TOKEN_PATTERN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
EVIDENCE_FIELDS = (
    "name",
    "org_unit",
    "research_areas",
    "summary",
    "tags",
)
GRAPH_KEYS = ("graph_score", "graph_paths", "graph_evidence", "paths")
DOMAIN_KEYS = ("domain_score", "domain_evidence", "sources", "evidence")


@dataclass(frozen=True)
class RerankResult:
    """Deterministic rerank score and explainable local evidence for one item."""

    item_id: Any
    rerank_score: float
    rerank_evidence: dict[str, Any] = field(default_factory=dict)


class BaseRerankerProvider(Protocol):
    """Provider interface for local or future external reranking."""

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> list[RerankResult]:
        """Return one rerank result for every candidate, preserving input order."""
        ...


class DisabledRerankerProvider:
    """Neutral provider used when reranking is off."""

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> list[RerankResult]:
        return [
            RerankResult(
                item_id=candidate.get("item_id"),
                rerank_score=0.0,
                rerank_evidence={"provider": "disabled", "reason": "reranker disabled"},
            )
            for candidate in candidates
        ]


class HeuristicRerankerProvider:
    """Offline deterministic reranker based on query, profile, graph, and domain evidence."""

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> list[RerankResult]:
        query_tokens = _tokenize(query)
        results: list[RerankResult] = []
        for candidate in candidates:
            field_matches = _field_matches(query_tokens, candidate)
            text_score = _text_overlap_score(query_tokens, field_matches)
            graph_score = _graph_evidence_score(candidate)
            domain_score = _domain_evidence_score(candidate)
            rerank_score = round(
                min(1.0, (0.70 * text_score) + (0.20 * graph_score) + (0.10 * domain_score)),
                4,
            )
            results.append(
                RerankResult(
                    item_id=candidate.get("item_id"),
                    rerank_score=rerank_score,
                    rerank_evidence={
                        "provider": "heuristic",
                        "matched_fields": field_matches,
                        "text_overlap_score": round(text_score, 4),
                        "graph_evidence_score": round(graph_score, 4),
                        "domain_evidence_score": round(domain_score, 4),
                    },
                )
            )
        return results


class ExternalRerankerProvider:
    """Future external provider stub with local fallback and no implicit network calls."""

    def __init__(self, config: Settings | None = None, fallback: BaseRerankerProvider | None = None):
        self.config = config or settings
        self.fallback = fallback or HeuristicRerankerProvider()

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> list[RerankResult]:
        if not self._has_required_config():
            logger.warning("External reranker config missing, using heuristic fallback")
            return self.fallback.rerank(query, candidates)

        try:
            provider_results = self._call_provider(query, candidates)
            return self._normalize_provider_results(provider_results, candidates)
        except NotImplementedError:
            logger.warning("External reranker not implemented, using heuristic fallback")
            return self.fallback.rerank(query, candidates)
        except ValueError as exc:
            logger.warning("External reranker returned invalid results, using heuristic fallback: %s", exc)
            return self.fallback.rerank(query, candidates)

    def _has_required_config(self) -> bool:
        return all(
            [
                self.config.RERANK_PROVIDER,
                self.config.RERANK_MODEL,
                self.config.RERANK_BASE_URL,
                self.config.RERANK_API_KEY,
            ]
        )

    def _call_provider(
        self,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        raise NotImplementedError("External reranker transport is not implemented")

    def _normalize_provider_results(
        self,
        provider_results: Any,
        candidates: list[dict[str, Any]],
    ) -> list[RerankResult]:
        if not isinstance(provider_results, list) or len(provider_results) != len(candidates):
            raise ValueError("External reranker returned malformed result list")

        normalized: list[RerankResult] = []
        for provider_result, candidate in zip(provider_results, candidates):
            if not isinstance(provider_result, dict):
                raise ValueError("External reranker result must be an object")
            score = provider_result.get("rerank_score")
            if not isinstance(score, (int, float)):
                raise ValueError("External reranker score must be numeric")
            normalized.append(
                RerankResult(
                    item_id=provider_result.get("item_id", candidate.get("item_id")),
                    rerank_score=round(max(0.0, min(1.0, float(score))), 4),
                    rerank_evidence={
                        "provider": self.config.RERANK_PROVIDER,
                        **_safe_dict(provider_result.get("rerank_evidence")),
                    },
                )
            )
        return normalized


class RerankerService:
    """Offline-first service that annotates candidates with rerank scores and evidence."""

    def __init__(
        self,
        config: Settings | None = None,
        provider: BaseRerankerProvider | None = None,
    ):
        self.config = config or settings
        self.provider = provider or self._build_provider()

    def rerank(self, query: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not candidates:
            return []

        limited_candidates = candidates[: self.config.RERANK_TOP_N]
        untouched_candidates = candidates[self.config.RERANK_TOP_N :]
        results = self.provider.rerank(query, limited_candidates)
        if len(results) != len(limited_candidates):
            logger.warning("Reranker result count mismatch, using heuristic fallback")
            results = HeuristicRerankerProvider().rerank(query, limited_candidates)

        reranked = []
        for candidate, result in zip(limited_candidates, results):
            updated = dict(candidate)
            updated["rerank_score"] = result.rerank_score
            updated["rerank_evidence"] = result.rerank_evidence
            reranked.append(updated)

        for candidate in untouched_candidates:
            updated = dict(candidate)
            updated.setdefault("rerank_score", 0.0)
            updated.setdefault(
                "rerank_evidence",
                {"provider": "not_reranked", "reason": "outside rerank top_n"},
            )
            reranked.append(updated)
        return reranked

    def _build_provider(self) -> BaseRerankerProvider:
        provider_name = (self.config.RERANK_PROVIDER or "heuristic").lower()
        if not self.config.ENABLE_RERANKER or provider_name == "disabled":
            return DisabledRerankerProvider()
        if provider_name == "heuristic":
            return HeuristicRerankerProvider()
        return ExternalRerankerProvider(self.config)


def _tokenize(text: Any) -> set[str]:
    normalized = str(text or "").lower()
    return {token for token in TOKEN_PATTERN.findall(normalized) if token.strip()}


def _candidate_field_text(candidate: dict[str, Any], field_name: str) -> str:
    value = candidate.get(field_name)
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    if isinstance(value, dict):
        return " ".join(str(item) for item in value.values())
    return str(value or "")


def _field_matches(
    query_tokens: set[str],
    candidate: dict[str, Any],
) -> dict[str, list[str]]:
    matches: dict[str, list[str]] = {}
    for field_name in EVIDENCE_FIELDS:
        field_tokens = _tokenize(_candidate_field_text(candidate, field_name))
        overlap = sorted(query_tokens & field_tokens)
        if overlap:
            matches[field_name] = overlap
    return matches


def _text_overlap_score(query_tokens: set[str], field_matches: dict[str, list[str]]) -> float:
    if not query_tokens:
        return 0.0
    matched_tokens = {token for tokens in field_matches.values() for token in tokens}
    coverage = len(matched_tokens) / len(query_tokens)
    field_bonus = min(0.2, 0.05 * len(field_matches))
    return min(1.0, coverage + field_bonus)


def _graph_evidence_score(candidate: dict[str, Any]) -> float:
    graph_score = candidate.get("graph_score")
    if isinstance(graph_score, (int, float)):
        return max(0.0, min(1.0, float(graph_score)))
    return 1.0 if any(candidate.get(key) for key in GRAPH_KEYS if key != "graph_score") else 0.0


def _domain_evidence_score(candidate: dict[str, Any]) -> float:
    domain_score = candidate.get("domain_score")
    if isinstance(domain_score, (int, float)):
        return max(0.0, min(1.0, float(domain_score)))
    evidence_hits = sum(1 for key in DOMAIN_KEYS if candidate.get(key))
    return min(1.0, evidence_hits * 0.25)


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}
