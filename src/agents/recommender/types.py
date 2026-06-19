from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class UserProfile:
    raw_text: str
    interests: list[str] = field(default_factory=list)
    target_locations: list[str] = field(default_factory=list)
    preferred_titles: list[str] = field(default_factory=list)
    degree_goals: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    source: str = "text"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProfessorRecommendation:
    name: str
    university_name: str
    org_unit_name: str
    title: str | None
    score: float
    matched_terms: list[str]
    evidence_urls: list[str]
    reasons: list[str]
    research_areas: str | None = None
    enrollment_pref: str | None = None
    homepage: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrgUnitRecommendation:
    university_name: str
    org_unit_name: str
    score: float
    matched_terms: list[str]
    evidence_urls: list[str]
    reasons: list[str]
    representative_professors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SchoolRecommendation:
    university_name: str
    location: str | None
    score: float
    matched_terms: list[str]
    evidence_urls: list[str]
    reasons: list[str]
    representative_org_units: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecommendationResult:
    profile: UserProfile
    schools: list[SchoolRecommendation]
    org_units: list[OrgUnitRecommendation]
    professors: list[ProfessorRecommendation]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile.to_dict(),
            "schools": [item.to_dict() for item in self.schools],
            "org_units": [item.to_dict() for item in self.org_units],
            "professors": [item.to_dict() for item in self.professors],
        }


@dataclass(frozen=True)
class GraphBuildSummary:
    graph_db_path: str
    source_count: int
    indexed_sources: int
    skipped_sources: int
    nodes_written: int
    edges_written: int
    terms_written: int
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
