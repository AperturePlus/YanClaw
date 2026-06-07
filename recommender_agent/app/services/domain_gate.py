from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session, select

from app.models.item import Item


UNKNOWN_DOMAIN = "general_unknown"
DEFAULT_ONTOLOGY_PATH = Path(__file__).resolve().parents[2] / "configs" / "discipline_ontology.yaml"
Candidate = dict[str, object]


@dataclass(frozen=True)
class DomainInference:
    """Domain inferred from query text and parsed intent keywords."""

    domain: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class DomainGateResult:
    """Result of scoring or gating candidates by a discipline domain."""

    candidates: list[Candidate]
    domain: str
    domain_confidence: float
    fallback_reason: str | None = None


class DomainGateService:
    """Infer discipline domains and softly score recommendation candidates."""

    def __init__(
        self,
        ontology_path: str | Path | None = None,
        min_confidence: float = 0.3,
        mode: str = "soft",
        fallback_on_empty: bool = True,
    ) -> None:
        self.ontology_path: Path = Path(ontology_path) if ontology_path else DEFAULT_ONTOLOGY_PATH
        self.min_confidence: float = min_confidence
        self.mode: str = mode
        self.fallback_on_empty: bool = fallback_on_empty
        self.ontology: dict[str, dict[str, list[str]]] = self._load_ontology(self.ontology_path)

    def infer_domain(
        self,
        query_text: str = "",
        intent_keywords: Iterable[str] | None = None,
    ) -> DomainInference:
        """Infer the most likely discipline domain from query and intent terms."""
        searchable_text = self._combine_text([query_text, *(intent_keywords or [])])
        if not searchable_text:
            return DomainInference(domain=UNKNOWN_DOMAIN, confidence=0.0)

        scored: dict[str, tuple[float, list[str]]] = {}
        for domain, spec in self.ontology.items():
            if domain == UNKNOWN_DOMAIN:
                continue
            score, evidence = self._score_text_against_domain(searchable_text, spec)
            scored[domain] = (score, evidence)

        if not scored:
            return DomainInference(domain=UNKNOWN_DOMAIN, confidence=0.0)

        best_domain, (best_score, evidence) = max(scored.items(), key=lambda item: item[1][0])
        total_score = sum(score for score, _ in scored.values())
        confidence = best_score / total_score if total_score else 0.0
        if best_score <= 0 or confidence < self.min_confidence:
            return DomainInference(
                domain=UNKNOWN_DOMAIN,
                confidence=0.0,
                scores={domain: score for domain, (score, _) in scored.items()},
            )

        return DomainInference(
            domain=best_domain,
            confidence=round(confidence, 4),
            evidence=evidence,
            scores={domain: score for domain, (score, _) in scored.items()},
        )

    def score_candidates(
        self,
        candidates: list[Candidate],
        query_domain: DomainInference | str,
        session: Session | None = None,
    ) -> DomainGateResult:
        """Add domain_score/domain_match/domain_evidence to candidates.

        Soft mode never removes candidates. Hard mode is supported as a foundation, but
        if every candidate would be removed it falls back to the pre-gate candidates.
        """
        inference = self._coerce_domain(query_domain)
        if inference.domain == UNKNOWN_DOMAIN or inference.confidence <= 0:
            return DomainGateResult(
                candidates=[
                    self._with_domain_metadata(
                        candidate,
                        score=0.0,
                        is_match=False,
                        evidence=[],
                        confidence=0.0,
                    )
                    for candidate in candidates
                ],
                domain=UNKNOWN_DOMAIN,
                domain_confidence=0.0,
            )

        domain_spec = self.ontology.get(inference.domain, {})
        enriched = [
            self._score_candidate(candidate, domain_spec, inference.confidence, session)
            for candidate in candidates
        ]

        if self.mode != "hard":
            return DomainGateResult(
                candidates=enriched,
                domain=inference.domain,
                domain_confidence=inference.confidence,
            )

        matched = [candidate for candidate in enriched if candidate["domain_match"]]
        if matched or not self.fallback_on_empty:
            return DomainGateResult(
                candidates=matched,
                domain=inference.domain,
                domain_confidence=inference.confidence,
            )

        fallback_candidates = [
            {**candidate, "domain_fallback_reason": "all_candidates_filtered_by_domain_gate"}
            for candidate in enriched
        ]
        return DomainGateResult(
            candidates=fallback_candidates,
            domain=inference.domain,
            domain_confidence=inference.confidence,
            fallback_reason="all_candidates_filtered_by_domain_gate",
        )

    def _score_candidate(
        self,
        candidate: Candidate,
        domain_spec: dict[str, list[str]],
        domain_confidence: float,
        session: Session | None,
    ) -> Candidate:
        candidate_text = self._candidate_text(candidate, session)
        raw_score, evidence = self._score_text_against_domain(candidate_text, domain_spec)
        normalized_score = min(raw_score / 6.0, 1.0) if raw_score else 0.0
        return self._with_domain_metadata(
            candidate,
            score=round(normalized_score, 4),
            is_match=normalized_score > 0,
            evidence=evidence,
            confidence=domain_confidence,
        )

    def _candidate_text(self, candidate: Candidate, session: Session | None) -> str:
        fields = [
            candidate.get("research_areas"),
            candidate.get("org_unit"),
            candidate.get("tags"),
            candidate.get("summary"),
            candidate.get("description"),
            candidate.get("title"),
        ]

        item_id = candidate.get("item_id")
        if session is not None and isinstance(item_id, int):
            item = session.exec(select(Item).where(Item.id == item_id)).first()
            if item is not None:
                fields.extend(
                    [
                        item.research_areas,
                        item.org_unit,
                        item.tags,
                        item.description,
                        item.title,
                    ]
                )
        return self._combine_text(fields)

    def _score_text_against_domain(
        self,
        text: str,
        domain_spec: dict[str, list[str]],
    ) -> tuple[float, list[str]]:
        score = 0.0
        evidence: list[str] = []
        weighted_fields = {
            "aliases": 1.0,
            "research_terms": 1.5,
            "org_unit_hints": 1.25,
        }
        for field_name, weight in weighted_fields.items():
            for term in domain_spec.get(field_name, []):
                if self._contains_term(text, term):
                    score += weight
                    evidence.append(f"{field_name}:{term}")
        return score, evidence

    def _with_domain_metadata(
        self,
        candidate: Candidate,
        score: float,
        is_match: bool,
        evidence: list[str],
        confidence: float,
    ) -> Candidate:
        enriched = dict(candidate)
        enriched["domain_score"] = score
        enriched["domain_match"] = is_match
        enriched["domain_evidence"] = evidence
        enriched["domain_confidence"] = confidence
        return enriched

    def _coerce_domain(self, query_domain: DomainInference | str) -> DomainInference:
        if isinstance(query_domain, DomainInference):
            return query_domain
        if query_domain == UNKNOWN_DOMAIN:
            return DomainInference(domain=UNKNOWN_DOMAIN, confidence=0.0)
        return DomainInference(domain=query_domain, confidence=1.0)

    def _load_ontology(self, path: Path) -> dict[str, dict[str, list[str]]]:
        text = path.read_text(encoding="utf-8")
        return self._parse_simple_ontology(text)

    def _parse_simple_ontology(self, text: str) -> dict[str, dict[str, list[str]]]:
        domains: dict[str, dict[str, list[str]]] = {}
        current_domain: str | None = None
        current_field: str | None = None
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped == "domains:":
                continue
            if line.startswith("  ") and not line.startswith("    ") and stripped.endswith(":"):
                current_domain = stripped[:-1]
                domains[current_domain] = {}
                current_field = None
                continue
            if current_domain and line.startswith("    ") and not line.startswith("      "):
                key, _, value = stripped.partition(":")
                current_field = key
                field_value = value.strip()
                if field_value == "[]":
                    domains[current_domain][key] = []
                elif field_value:
                    domains[current_domain][key] = [field_value]
                else:
                    domains[current_domain][key] = []
                continue
            if current_domain and current_field and stripped.startswith("- "):
                domains[current_domain].setdefault(current_field, []).append(stripped[2:])
        return domains

    def _combine_text(self, parts: Iterable[object]) -> str:
        return " ".join(str(part) for part in parts if part).lower()

    def _contains_term(self, text: str, term: str) -> bool:
        normalized_term = term.strip().lower()
        return bool(normalized_term) and normalized_term in text
