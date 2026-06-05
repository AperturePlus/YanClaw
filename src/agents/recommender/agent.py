from __future__ import annotations

import json
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from agents.crawler.config import CrawlerSettings
from agents.recommender.db import repository
from agents.recommender.graph_agent import KnowledgeGraphAgent
from agents.recommender.text import tokenize, top_terms
from agents.recommender.types import (
    OrgUnitRecommendation,
    ProfessorRecommendation,
    RecommendationResult,
    SchoolRecommendation,
    UserProfile,
)
from runtime.llm import LLMClient
from runtime.logger import get_logger


_KNOWN_LOCATIONS = (
    "北京",
    "上海",
    "天津",
    "重庆",
    "广州",
    "深圳",
    "杭州",
    "南京",
    "武汉",
    "成都",
    "西安",
    "长沙",
    "合肥",
    "哈尔滨",
    "大连",
    "青岛",
    "厦门",
    "苏州",
    "济南",
    "郑州",
)


@dataclass(frozen=True)
class _ProfessorCandidate:
    key: str
    node: dict[str, Any]
    score: float
    matched_terms: list[str]


class RecommendationAgent:
    """Recommend universities, org units, and advisors from the local knowledge graph."""

    def __init__(
        self,
        *,
        settings: CrawlerSettings,
        graph_db_path: Path | None = None,
        graph_agent: KnowledgeGraphAgent | None = None,
    ) -> None:
        self.settings = settings
        self.graph_db_path = Path(graph_db_path or settings.knowledge_graph_db_path)
        self.graph_agent = graph_agent or KnowledgeGraphAgent(
            settings=settings,
            graph_db_path=self.graph_db_path,
        )
        self.logger = get_logger("recommender.agent")

    async def recommend(
        self,
        *,
        text: str | None = None,
        file: Path | None = None,
        top_schools: int | None = None,
        top_org_units: int | None = None,
        top_professors: int | None = None,
        auto_build: bool = True,
    ) -> RecommendationResult:
        raw_text, source = self._load_input(text=text, file=file)
        profile = await self.parse_user_profile(raw_text, source=source)
        if auto_build:
            await self._ensure_graph()

        async with repository.connect_graph(self.graph_db_path) as conn:
            if not await repository.graph_has_documents(conn):
                return RecommendationResult(profile=profile, schools=[], org_units=[], professors=[])

            query_terms = _unique_terms(
                top_terms(
                    " ".join(
                        [
                            profile.raw_text,
                            " ".join(profile.interests),
                            " ".join(profile.degree_goals),
                            " ".join(profile.preferred_titles),
                            " ".join(profile.target_locations),
                        ]
                    ),
                    limit=80,
                )
                + tokenize(profile.raw_text)[:120]
            )
            scores = await repository.search_by_terms(conn, query_terms, limit=300)
            score_by_key = {item.node_key: float(item.score) for item in scores}
            type_by_key = {item.node_key: item.node_type for item in scores}
            matched_by_key = {item.node_key: set(item.matched_terms) for item in scores}

            concept_keys = [item.node_key for item in scores if item.node_type == "concept"]
            linked_by_professor = await repository.find_professors_linked_to_concepts(
                conn,
                concept_keys,
                limit=300,
            )
            for professor_key, linked_concepts in linked_by_professor.items():
                for concept in linked_concepts:
                    score_by_key[professor_key] = score_by_key.get(professor_key, 0.0) + (
                        score_by_key.get(concept, 0.0) * 0.8
                    )
                    type_by_key[professor_key] = "professor"
                    matched_by_key.setdefault(professor_key, set()).update(
                        matched_by_key.get(concept, set())
                    )

            professor_keys = [
                key
                for key, node_type in type_by_key.items()
                if node_type == "professor" and score_by_key.get(key, 0.0) > 0
            ]
            professor_keys = sorted(professor_keys, key=lambda key: (-score_by_key[key], key))[:200]
            professor_nodes = await repository.get_nodes(conn, professor_keys)
            context_keys = _context_keys(professor_nodes)
            context_nodes = await repository.get_nodes(conn, context_keys)

            candidates = [
                _ProfessorCandidate(
                    key=key,
                    node=node,
                    score=score_by_key.get(key, 0.0),
                    matched_terms=sorted(matched_by_key.get(key, set())),
                )
                for key, node in professor_nodes.items()
            ]
            professors = self._rank_professors(
                profile=profile,
                candidates=candidates,
                context_nodes=context_nodes,
                score_by_key=score_by_key,
                matched_by_key=matched_by_key,
                limit=int(top_professors or self.settings.recommend_top_professors),
            )
            org_units = self._rank_org_units(
                professors=professors,
                professor_nodes=professor_nodes,
                context_nodes=context_nodes,
                score_by_key=score_by_key,
                matched_by_key=matched_by_key,
                limit=int(top_org_units or self.settings.recommend_top_org_units),
            )
            schools = self._rank_schools(
                professors=professors,
                org_units=org_units,
                professor_nodes=professor_nodes,
                context_nodes=context_nodes,
                score_by_key=score_by_key,
                matched_by_key=matched_by_key,
                limit=int(top_schools or self.settings.recommend_top_schools),
            )
            return RecommendationResult(
                profile=profile,
                schools=schools,
                org_units=org_units,
                professors=professors,
            )

    async def parse_user_profile(self, raw_text: str, *, source: str = "text") -> UserProfile:
        if self.settings.openai_api_key:
            try:
                return await self._parse_profile_with_llm(raw_text, source=source)
            except Exception:
                self.logger.exception("LLM profile parsing failed; falling back to local parser")
        return self._parse_profile_locally(raw_text, source=source)

    async def _parse_profile_with_llm(self, raw_text: str, *, source: str) -> UserProfile:
        llm = LLMClient(
            self.settings.openai_base_url,
            self.settings.openai_api_key,
            self.settings.openai_model,
            max_concurrent=self.settings.llm_max_concurrent,
            min_interval=self.settings.llm_min_interval_seconds,
            timeout_seconds=self.settings.llm_timeout_seconds,
            temperature=self.settings.llm_temperature,
            top_p=self.settings.llm_top_p,
            seed=self.settings.llm_seed,
            max_rounds=1,
        )
        response = await llm.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Extract a recommendation profile from user text. "
                        "Return strict JSON only with keys: interests, target_locations, "
                        "preferred_titles, degree_goals, constraints. Values must be arrays of strings."
                    ),
                },
                {"role": "user", "content": raw_text[:12000]},
            ],
            max_tokens=800,
        )
        parsed = _parse_json_object(response.content)
        if not parsed:
            return self._parse_profile_locally(raw_text, source=source)
        return UserProfile(
            raw_text=raw_text,
            interests=_string_list(parsed.get("interests")) or top_terms(raw_text, limit=16),
            target_locations=_string_list(parsed.get("target_locations")),
            preferred_titles=_string_list(parsed.get("preferred_titles")),
            degree_goals=_string_list(parsed.get("degree_goals")),
            constraints=_string_list(parsed.get("constraints")),
            source=source,
        )

    def _parse_profile_locally(self, raw_text: str, *, source: str) -> UserProfile:
        lowered = raw_text.lower()
        degree_goals: list[str] = []
        if any(term in raw_text for term in ("博士", "博士生", "读博", "phd", "PhD")):
            degree_goals.append("博士")
        if any(term in raw_text for term in ("硕士", "研究生", "master", "Master")):
            degree_goals.append("硕士")

        preferred_titles: list[str] = []
        for title in ("院士", "教授", "副教授", "研究员", "博士生导师", "硕士生导师"):
            if title in raw_text:
                preferred_titles.append(title)
        if "advisor" in lowered or "supervisor" in lowered:
            preferred_titles.append("导师")

        locations = [location for location in _KNOWN_LOCATIONS if location in raw_text]
        constraints = []
        if any(term in raw_text for term in ("留学", "出国", "海外", "国际")):
            constraints.append("国际合作")
        if any(term in raw_text for term in ("就业", "产业", "工程", "实践")):
            constraints.append("产业实践")

        return UserProfile(
            raw_text=raw_text,
            interests=top_terms(raw_text, limit=24),
            target_locations=locations,
            preferred_titles=_unique_terms(preferred_titles),
            degree_goals=_unique_terms(degree_goals),
            constraints=constraints,
            source=source,
        )

    async def _ensure_graph(self) -> None:
        if not self.graph_db_path.exists():
            await self.graph_agent.build(rebuild=False)
            return
        async with repository.connect_graph(self.graph_db_path) as conn:
            has_documents = await repository.graph_has_documents(conn)
        if not has_documents:
            await self.graph_agent.build(rebuild=False)

    def _rank_professors(
        self,
        *,
        profile: UserProfile,
        candidates: list[_ProfessorCandidate],
        context_nodes: dict[str, dict[str, Any]],
        score_by_key: dict[str, float],
        matched_by_key: dict[str, set[str]],
        limit: int,
    ) -> list[ProfessorRecommendation]:
        if not candidates:
            return []
        max_text = max((candidate.score for candidate in candidates), default=1.0)
        context_scores = [
            score_by_key.get(key, 0.0)
            for candidate in candidates
            for key in _payload_context_keys(candidate.node.get("payload") or {})
        ]
        max_context = max(context_scores, default=1.0)
        ranked: list[ProfessorRecommendation] = []
        for candidate in candidates:
            payload = candidate.node.get("payload") or {}
            org_keys = _list(payload.get("org_unit_keys"))
            university_key = str(payload.get("university_key") or "")
            org_context_score = max(
                [score_by_key.get(university_key, 0.0)]
                + [score_by_key.get(key, 0.0) for key in org_keys]
                + [0.0]
            )
            text_score = _normalize(candidate.score, max_text)
            completeness = _profile_completeness(payload)
            title_score = _title_score(profile, payload)
            org_school_score = _normalize(org_context_score, max_context)
            constraint_score = _constraint_score(profile, payload)
            final = round(
                100
                * (
                    (0.55 * text_score)
                    + (0.15 * completeness)
                    + (0.10 * title_score)
                    + (0.15 * org_school_score)
                    + (0.05 * constraint_score)
                ),
                2,
            )
            matched_terms = sorted(
                set(candidate.matched_terms)
                | set(matched_by_key.get(university_key, set()))
                | {term for key in org_keys for term in matched_by_key.get(key, set())}
            )
            ranked.append(
                ProfessorRecommendation(
                    name=str(payload.get("name") or candidate.node.get("name") or ""),
                    university_name=str(payload.get("university_name") or ""),
                    org_unit_name=str(payload.get("org_unit_name") or ""),
                    title=str(payload.get("title") or "") or None,
                    score=min(100.0, final),
                    matched_terms=matched_terms[:16],
                    evidence_urls=_evidence_urls(payload),
                    reasons=_professor_reasons(profile, payload, matched_terms),
                    research_areas=str(payload.get("research_areas") or "") or None,
                    enrollment_pref=str(payload.get("enrollment_pref") or "") or None,
                    homepage=str(payload.get("homepage") or "") or None,
                )
            )
        return sorted(ranked, key=lambda item: (-item.score, item.university_name, item.org_unit_name, item.name))[
            : max(0, limit)
        ]

    def _rank_org_units(
        self,
        *,
        professors: list[ProfessorRecommendation],
        professor_nodes: dict[str, dict[str, Any]],
        context_nodes: dict[str, dict[str, Any]],
        score_by_key: dict[str, float],
        matched_by_key: dict[str, set[str]],
        limit: int,
    ) -> list[OrgUnitRecommendation]:
        grouped: dict[str, dict[str, Any]] = {}
        rec_by_name = {(item.university_name, item.org_unit_name, item.name): item for item in professors}
        for node in professor_nodes.values():
            payload = node.get("payload") or {}
            rec = rec_by_name.get(
                (
                    str(payload.get("university_name") or ""),
                    str(payload.get("org_unit_name") or ""),
                    str(payload.get("name") or ""),
                )
            )
            if rec is None:
                continue
            for org_key in _list(payload.get("org_unit_keys")):
                org_node = context_nodes.get(org_key, {})
                org_payload = org_node.get("payload") or {}
                item = grouped.setdefault(
                    org_key,
                    {
                        "university_name": str(org_payload.get("university_name") or rec.university_name),
                        "org_unit_name": str(org_payload.get("org_unit_name") or rec.org_unit_name),
                        "scores": [],
                        "matched": set(matched_by_key.get(org_key, set())),
                        "urls": [],
                        "professors": [],
                    },
                )
                item["scores"].append(rec.score)
                item["matched"].update(rec.matched_terms)
                item["urls"].extend(rec.evidence_urls)
                item["professors"].append(rec.name)
                if org_node.get("source_url"):
                    item["urls"].append(str(org_node["source_url"]))

        result: list[OrgUnitRecommendation] = []
        for org_key, item in grouped.items():
            scores = sorted([float(score) for score in item["scores"]], reverse=True)
            own_boost = min(12.0, score_by_key.get(org_key, 0.0) * 2.0)
            score = round(min(100.0, (sum(scores[:3]) / max(1, len(scores[:3]))) + own_boost), 2)
            result.append(
                OrgUnitRecommendation(
                    university_name=item["university_name"],
                    org_unit_name=item["org_unit_name"],
                    score=score,
                    matched_terms=sorted(item["matched"])[:16],
                    evidence_urls=_unique_terms(item["urls"])[:8],
                    reasons=[
                        f"关联导师匹配度高：{', '.join(_unique_terms(item['professors'])[:3])}",
                        "院系方向由导师研究方向和招生偏好聚合得到",
                    ],
                    representative_professors=_unique_terms(item["professors"])[:5],
                )
            )
        return sorted(result, key=lambda item: (-item.score, item.university_name, item.org_unit_name))[
            : max(0, limit)
        ]

    def _rank_schools(
        self,
        *,
        professors: list[ProfessorRecommendation],
        org_units: list[OrgUnitRecommendation],
        professor_nodes: dict[str, dict[str, Any]],
        context_nodes: dict[str, dict[str, Any]],
        score_by_key: dict[str, float],
        matched_by_key: dict[str, set[str]],
        limit: int,
    ) -> list[SchoolRecommendation]:
        org_by_name = {(item.university_name, item.org_unit_name): item for item in org_units}
        grouped: dict[str, dict[str, Any]] = {}
        for node in professor_nodes.values():
            payload = node.get("payload") or {}
            university_key = str(payload.get("university_key") or "")
            if not university_key:
                continue
            professor = next(
                (
                    item
                    for item in professors
                    if item.name == str(payload.get("name") or "")
                    and item.university_name == str(payload.get("university_name") or "")
                ),
                None,
            )
            if professor is None:
                continue
            university_node = context_nodes.get(university_key, {})
            university_payload = university_node.get("payload") or {}
            item = grouped.setdefault(
                university_key,
                {
                    "university_name": str(payload.get("university_name") or ""),
                    "location": str(university_payload.get("location") or payload.get("location") or "") or None,
                    "scores": [],
                    "matched": set(matched_by_key.get(university_key, set())),
                    "urls": [],
                    "org_units": [],
                },
            )
            item["scores"].append(professor.score)
            item["matched"].update(professor.matched_terms)
            item["urls"].extend(professor.evidence_urls)
            org_rec = org_by_name.get((professor.university_name, professor.org_unit_name))
            if org_rec is not None:
                item["org_units"].append(org_rec.org_unit_name)

        result: list[SchoolRecommendation] = []
        for university_key, item in grouped.items():
            scores = sorted([float(score) for score in item["scores"]], reverse=True)
            own_boost = min(10.0, score_by_key.get(university_key, 0.0) * 2.0)
            score = round(min(100.0, (sum(scores[:5]) / max(1, len(scores[:5]))) + own_boost), 2)
            result.append(
                SchoolRecommendation(
                    university_name=item["university_name"],
                    location=item["location"],
                    score=score,
                    matched_terms=sorted(item["matched"])[:16],
                    evidence_urls=_unique_terms(item["urls"])[:8],
                    reasons=[
                        f"校内匹配导师数量：{len(scores)}",
                        "学校得分由导师匹配度、院系聚合和地点约束共同决定",
                    ],
                    representative_org_units=_unique_terms(item["org_units"])[:5],
                )
            )
        return sorted(result, key=lambda item: (-item.score, item.university_name))[: max(0, limit)]

    def _load_input(self, *, text: str | None, file: Path | None) -> tuple[str, str]:
        if text and file:
            raise ValueError("Use either text or file, not both")
        if file:
            return read_user_file(file), str(file)
        raw = str(text or "").strip()
        if not raw:
            raise ValueError("Recommendation input text is empty")
        return raw, "text"


def read_user_file(path: str | Path) -> str:
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    if suffix in {".txt", ".md", ".markdown"}:
        return file_path.read_text(encoding="utf-8")
    if suffix == ".docx":
        return _read_docx_text(file_path)
    if suffix == ".pdf":
        return _read_pdf_text(file_path)
    raise ValueError(f"Unsupported recommendation input file type: {suffix}")


def _read_docx_text(path: Path) -> str:
    try:
        from docx import Document  # type: ignore

        document = Document(path)
        return "\n".join(paragraph.text for paragraph in document.paragraphs)
    except ImportError:
        # Lightweight OOXML fallback keeps text resumes usable even before optional deps are installed.
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml")
        root = ElementTree.fromstring(xml)
        texts = []
        for node in root.iter():
            if node.tag.endswith("}t") and node.text:
                texts.append(node.text)
        return "\n".join(texts)


def _read_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as error:
        raise RuntimeError("PDF input requires the optional pypdf package") from error
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _context_keys(nodes: dict[str, dict[str, Any]]) -> list[str]:
    keys: list[str] = []
    for node in nodes.values():
        payload = node.get("payload") or {}
        keys.extend(_payload_context_keys(payload))
    return _unique_terms(keys)


def _payload_context_keys(payload: dict[str, Any]) -> list[str]:
    return _unique_terms([str(payload.get("university_key") or "")] + _list(payload.get("org_unit_keys")))


def _profile_completeness(payload: dict[str, Any]) -> float:
    fields = ("research_areas", "bio", "enrollment_pref", "publications", "email", "homepage")
    filled = sum(1 for field in fields if str(payload.get(field) or "").strip())
    return filled / len(fields)


def _title_score(profile: UserProfile, payload: dict[str, Any]) -> float:
    title = str(payload.get("title") or "")
    enrollment = str(payload.get("enrollment_pref") or "")
    if "博士" in profile.degree_goals and "博士" in enrollment:
        return 1.0
    if "硕士" in profile.degree_goals and "硕士" in enrollment:
        return 1.0
    for preferred in profile.preferred_titles:
        if preferred and (preferred in title or preferred in enrollment):
            return 1.0
    if any(term in title for term in ("院士", "教授", "研究员")):
        return 0.7
    if title:
        return 0.45
    return 0.2


def _constraint_score(profile: UserProfile, payload: dict[str, Any]) -> float:
    if not profile.target_locations:
        return 0.5
    haystack = " ".join(
        [
            str(payload.get("location") or ""),
            str(payload.get("university_name") or ""),
            str(payload.get("org_unit_name") or ""),
        ]
    )
    return 1.0 if any(location in haystack for location in profile.target_locations) else 0.0


def _professor_reasons(profile: UserProfile, payload: dict[str, Any], matched_terms: list[str]) -> list[str]:
    reasons: list[str] = []
    if matched_terms:
        reasons.append("匹配关键词：" + "、".join(matched_terms[:6]))
    if payload.get("research_areas"):
        reasons.append("研究方向：" + str(payload["research_areas"])[:80])
    if payload.get("enrollment_pref"):
        reasons.append("招生偏好：" + str(payload["enrollment_pref"])[:80])
    if profile.target_locations and _constraint_score(profile, payload) > 0:
        reasons.append("地点符合用户偏好")
    if not reasons:
        reasons.append("导师公开资料与用户输入存在文本匹配")
    return reasons[:4]


def _evidence_urls(payload: dict[str, Any]) -> list[str]:
    return _unique_terms(
        [
            str(payload.get("homepage") or ""),
            str(payload.get("source_url") or ""),
            str(payload.get("external_link") or ""),
        ]
    )


def _normalize(value: float, maximum: float) -> float:
    if maximum <= 0:
        return 0.0
    return max(0.0, min(float(value) / float(maximum), 1.0))


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return _unique_terms(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str) and value.strip():
        return _unique_terms(re.split(r"[,，;；、\n]+", value))
    return []


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    return []


def _unique_terms(values: Any) -> list[str]:
    unique: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in unique:
            unique.append(text)
    return unique
