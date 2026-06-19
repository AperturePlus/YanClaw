from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

from agents.crawler.fetchers import Fetcher
from agents.crawler.heuristic_constants import (
    DEFAULT_ORG_UNIT_EXCLUDE_KEYWORDS,
    FACULTY_KEYWORDS,
    FACULTY_PAGE_TYPE_CATEGORY,
    FACULTY_PAGE_TYPE_ELITE,
    FACULTY_PAGE_TYPE_FULL,
    FACULTY_PAGE_TYPE_NOISE,
    FACULTY_PAGE_TYPE_UNKNOWN,
    ORG_UNIT_PAGE_KEYWORDS,
    _ACTIVE_TEXT_HINTS,
    _CATEGORY_PATTERNS,
    _COMMON_FACULTY_PATHS,
    _EXPLICIT_FACULTY_DIR_HINTS,
    _FACULTY_CATEGORY_TEXT_HINTS,
    _FACULTY_ELITE_TEXT_HINTS,
    _FACULTY_FULL_STRONG_TEXT_HINTS,
    _FACULTY_FULL_TEXT_HINTS,
    _FACULTY_LOGIN_HARD_REJECT_HINTS,
    _FACULTY_NAV_CONTEXT_HINTS,
    _FACULTY_NOISE_STEM_HINTS,
    _FACULTY_NOISE_TEXT_HINTS,
    _FACULTY_NOISE_TOKEN_HINTS,
    _FACULTY_NOISE_URL_HINTS,
    _FOCUS_AI_ASCII_TERMS,
    _FOCUS_AI_HINTS,
    _FOCUS_AI_HOST_LABELS,
    _FOCUS_COMPUTER_ASCII_TERMS,
    _FOCUS_COMPUTER_HINTS,
    _FOCUS_COMPUTER_HOST_LABELS,
    _FOCUS_ELECTRONICS_ASCII_TERMS,
    _FOCUS_ELECTRONICS_HINTS,
    _FOCUS_ELECTRONICS_HOST_LABELS,
    _FOCUS_SOFTWARE_ASCII_TERMS,
    _FOCUS_SOFTWARE_HINTS,
    _FOCUS_SOFTWARE_HOST_LABELS,
    _INTERMEDIATE_ORG_PATHS,
    _ORG_UNIT_EXCLUDE_KEYWORD_GROUPS,
    _ORG_UNIT_LISTING_NOISE_HINTS,
    _ORG_UNIT_LISTING_STRONG_HINTS,
    _PAGINATION_RE,
    _PROMOTIONAL_NOISE_TOKENS,
    _RETIRED_TEXT_HINTS,
    _RETIRED_URL_HINTS,
    _URL_RE,
)
from agents.crawler.models import OrgUnit


@dataclass(frozen=True)
class OrgUnitExclusionMatch:
    category: str
    keyword: str
    reason: str


def _org_unit_exclusion_match(
    *,
    name: str,
    kind: str | None = None,
    url: str | None = None,
    keywords: tuple[str, ...] | list[str] | None = None,
) -> OrgUnitExclusionMatch | None:
    active_keywords = tuple(
        str(keyword).strip()
        for keyword in (DEFAULT_ORG_UNIT_EXCLUDE_KEYWORDS if keywords is None else keywords)
        if str(keyword).strip()
    )
    if not active_keywords:
        return None

    text = _org_unit_exclusion_text(name=name, kind=kind, url=url)
    active = {_normalize_exclude_keyword(keyword) for keyword in active_keywords}

    for category, grouped_keywords in _ORG_UNIT_EXCLUDE_KEYWORD_GROUPS:
        for keyword in grouped_keywords:
            normalized = _normalize_exclude_keyword(keyword)
            if normalized not in active:
                continue
            if _matches_org_unit_exclude_keyword(text, normalized):
                return OrgUnitExclusionMatch(
                    category=category,
                    keyword=keyword,
                    reason=f"{category}:{keyword}",
                )

    grouped = {
        _normalize_exclude_keyword(keyword)
        for _, grouped_keywords in _ORG_UNIT_EXCLUDE_KEYWORD_GROUPS
        for keyword in grouped_keywords
    }
    for keyword in active_keywords:
        normalized = _normalize_exclude_keyword(keyword)
        if not normalized or normalized in grouped:
            continue
        if _matches_org_unit_exclude_keyword(text, normalized):
            return OrgUnitExclusionMatch(
                category="custom",
                keyword=keyword,
                reason=f"custom:{keyword}",
            )
    return None


def _should_exclude_org_unit(
    *,
    name: str,
    kind: str | None = None,
    url: str | None = None,
    keywords: tuple[str, ...] | list[str] | None = None,
) -> bool:
    return _org_unit_exclusion_match(name=name, kind=kind, url=url, keywords=keywords) is not None


def _org_unit_exclusion_text(*, name: str, kind: str | None, url: str | None) -> str:
    parsed = urlparse(str(url or ""))
    host = parsed.hostname or ""
    path = unquote(parsed.path or "")
    return f"{name or ''} {kind or ''} {host} {path}".lower()


def _normalize_exclude_keyword(keyword: str) -> str:
    return re.sub(r"\s+", " ", str(keyword or "").strip().lower())


def _matches_org_unit_exclude_keyword(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    if any(ord(char) > 127 for char in keyword):
        return keyword in text
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def _keyword_filter(links: list[str], keywords: tuple[str, ...]) -> list[str]:
    lowered = [(keyword, keyword.lower()) for keyword in keywords]
    return [
        link
        for link in links
        if any(keyword in link or ascii_keyword in link.lower() for keyword, ascii_keyword in lowered)
    ]


def _looks_like_faculty_page(url: str) -> bool:
    return bool(_keyword_filter([url], FACULTY_KEYWORDS))


def _iter_url_noise_tokens(url: str) -> list[str]:
    # Only tokenize the path: query strings frequently embed generic words such
    # as "news" inside parameter values (e.g. BUAA siteweaver detail pages use
    # urltype=news.NewsContentUrl) and would otherwise condemn legitimate
    # teacher-detail URLs as faculty noise.
    parsed = urlparse(url.lower())
    text = parsed.path or ""
    return [token for token in re.split(r"[^a-z0-9]+", text) if token]


def _matches_noise_stem(token: str) -> bool:
    for stem in _FACULTY_NOISE_STEM_HINTS:
        if not token.startswith(stem):
            continue
        suffix = token[len(stem) :]
        if not suffix or suffix.isdigit():
            return True
    return False


def _is_promotional_noise_url(url: str) -> bool:
    tokens = set(_iter_url_noise_tokens(url))
    if not tokens.intersection(_PROMOTIONAL_NOISE_TOKENS):
        return False
    lowered = (url or "").lower()
    if _is_explicit_faculty_directory_url(lowered) and _looks_like_faculty_page(lowered):
        return False
    return True


def _is_non_faculty_noise_url(url: str) -> bool:
    lowered = url.lower()
    if _is_promotional_noise_url(lowered):
        return True
    safe_url_hints = tuple(
        token for token in _FACULTY_NOISE_URL_HINTS if token.strip("/") not in _PROMOTIONAL_NOISE_TOKENS
    )
    if any(token in lowered for token in safe_url_hints):
        return True
    for token in _iter_url_noise_tokens(lowered):
        if token in _PROMOTIONAL_NOISE_TOKENS:
            continue
        if token in _FACULTY_NOISE_TOKEN_HINTS or _matches_noise_stem(token):
            return True
    # Dated news/article URLs are usually irrelevant for faculty extraction.
    path = urlparse(lowered).path
    if re.search(r"/20\d{2}/\d{2}(/\d{2})?/", path):
        return True
    return False


def _is_explicit_faculty_directory_url(url: str) -> bool:
    lowered = (url or "").lower()
    return any(token in lowered for token in _EXPLICIT_FACULTY_DIR_HINTS)


def _looks_like_org_unit_listing_url(url: str) -> bool:
    lowered = url.lower()
    if any(token in lowered for token in _ORG_UNIT_LISTING_NOISE_HINTS):
        return False
    return any(token in lowered for token in _ORG_UNIT_LISTING_STRONG_HINTS)


def _rank_faculty_page_candidates(links: list[str]) -> list[str]:
    if not links:
        return []

    strong_tokens = (
        "teacher",
        "faculty",
        "staff",
        "people",
        "directory",
        "list",
        "professor",
        "mentor",
        "szdw",
        "jsdw",
        "qzjs",
        "jzg",
        "jslb",
        "jsml",
        "jiaoshiliebiao",
        "jiaoshiml",
        "jiaoshimulu",
        "导师",
        "教师",
        "名录",
    )
    weak_tokens = (
        "lyys",
        "yuanshi",
        "academician",
        "jxms",
        "mingshi",
        "ldsr",
        "award",
        "honor",
        "rongyu",
        "yuan-shi",
        "famous-teacher",
    )

    def _score(link: str) -> int:
        lowered = link.lower()
        score = 0
        if any(token in lowered for token in strong_tokens):
            score += 6
        if any(token in lowered for token in weak_tokens):
            score -= 8
        if _is_pagination_link(link):
            score -= 3
        return score

    ranked = sorted(links, key=lambda link: (-_score(link), -len(link), link.lower()))
    deduped: list[str] = []
    seen: set[str] = set()
    for link in ranked:
        if link in seen:
            continue
        seen.add(link)
        deduped.append(link)
    return deduped


def _is_academician_showcase_page(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in ("lyys", "yuanshi", "academician", "yuan-shi"))


@dataclass(frozen=True)
class FacultyCandidateAssessment:
    url: str
    page_type: str
    score: int
    uncertain: bool
    hard_reject: bool
    reasons: tuple[str, ...] = ()
    anchor_text: str = ""
    heading_text: str = ""
    parent_tags_or_classes: tuple[str, ...] = ()
    link_order: int = 0


def _contains_any(text: str, hints: tuple[str, ...]) -> bool:
    lowered = (text or "").lower()
    return any(hint.lower() in lowered for hint in hints)


def _looks_like_active_roster_url(url: str) -> bool:
    tokens = set(_iter_url_noise_tokens(url))
    if not tokens or tokens.intersection({"djgz", "dqjs", "dangjian", "party"}):
        return False
    if "zzjs" not in tokens:
        return False
    return bool(tokens.intersection({"szjs", "szdw", "jsdw", "faculty", "teacher", "teachers"}))


def _looks_like_generic_faculty_section_url(url: str) -> bool:
    tokens = set(_iter_url_noise_tokens(url))
    return "szjs" in tokens and not _looks_like_active_roster_url(url)


def _assess_faculty_candidate(
    url: str,
    *,
    anchor_text: str = "",
    heading_text: str = "",
    parent_tags_or_classes: tuple[str, ...] = (),
    link_order: int = 0,
) -> FacultyCandidateAssessment:
    lowered_url = (url or "").lower()
    reasons: list[str] = []

    for token in _FACULTY_LOGIN_HARD_REJECT_HINTS:
        if token in lowered_url:
            return FacultyCandidateAssessment(
                url=url,
                page_type=FACULTY_PAGE_TYPE_NOISE,
                score=-100,
                uncertain=False,
                hard_reject=True,
                reasons=(f"hard_reject:{token}",),
                anchor_text=anchor_text,
                heading_text=heading_text,
                parent_tags_or_classes=parent_tags_or_classes,
                link_order=link_order,
            )

    signal_text = " ".join(
        [
            lowered_url,
            (anchor_text or "").lower(),
            (heading_text or "").lower(),
            " ".join((item or "").lower() for item in parent_tags_or_classes),
        ]
    )

    active_roster_url_hit = _looks_like_active_roster_url(url)
    full_hit = _contains_any(signal_text, _FACULTY_FULL_TEXT_HINTS) or active_roster_url_hit
    full_strong_hit = _contains_any(signal_text, _FACULTY_FULL_STRONG_TEXT_HINTS)
    category_hit = _contains_any(signal_text, _FACULTY_CATEGORY_TEXT_HINTS)
    elite_hit = _contains_any(signal_text, _FACULTY_ELITE_TEXT_HINTS)
    explicit_noise_url_hit = _is_non_faculty_noise_url(url)
    noise_hit = _contains_any(signal_text, _FACULTY_NOISE_TEXT_HINTS) or explicit_noise_url_hit
    nav_hit = _contains_any(signal_text, _FACULTY_NAV_CONTEXT_HINTS)

    score = 0
    if full_hit:
        score += 12
        reasons.append("active_roster_url_hit" if active_roster_url_hit else "full_hit")
    if category_hit:
        score += 7
        reasons.append("category_hit")
    if elite_hit:
        score += 5
        reasons.append("elite_hit")
    if nav_hit:
        score += 2
        reasons.append("nav_hit")
    if _looks_like_faculty_page(url) and (
        active_roster_url_hit
        or category_hit
        or elite_hit
        or not _looks_like_generic_faculty_section_url(url)
    ):
        score += 3
        reasons.append("url_faculty_hit")
    if _is_pagination_link(url):
        score -= 2
        reasons.append("pagination_penalty")
    if noise_hit:
        score -= 14
        reasons.append("noise_hit")
    if explicit_noise_url_hit:
        score -= 8
        reasons.append("explicit_noise_url_hit")

    page_type = FACULTY_PAGE_TYPE_UNKNOWN
    if explicit_noise_url_hit or (noise_hit and score <= 0):
        page_type = FACULTY_PAGE_TYPE_NOISE
    elif category_hit and (not full_strong_hit or _contains_any(f"{anchor_text} {heading_text}", _FACULTY_CATEGORY_TEXT_HINTS)):
        page_type = FACULTY_PAGE_TYPE_CATEGORY
    elif full_hit:
        page_type = FACULTY_PAGE_TYPE_FULL
    elif elite_hit:
        page_type = FACULTY_PAGE_TYPE_ELITE

    uncertain = False
    if page_type in {FACULTY_PAGE_TYPE_FULL, FACULTY_PAGE_TYPE_CATEGORY, FACULTY_PAGE_TYPE_ELITE}:
        uncertain = 3 <= score <= 7
    elif page_type == FACULTY_PAGE_TYPE_UNKNOWN:
        uncertain = score >= 2

    return FacultyCandidateAssessment(
        url=url,
        page_type=page_type,
        score=score,
        uncertain=uncertain,
        hard_reject=False,
        reasons=tuple(reasons),
        anchor_text=anchor_text,
        heading_text=heading_text,
        parent_tags_or_classes=parent_tags_or_classes,
        link_order=link_order,
    )


def _assess_structural_faculty_candidates(
    links: list[str],
    *,
    link_signals: tuple[Any, ...] | list[Any] | None = None,
) -> list[FacultyCandidateAssessment]:
    if not links:
        return []

    signal_map: dict[str, Any] = {}
    for signal in link_signals or ():
        signal_url = str(getattr(signal, "url", "") or "")
        if signal_url and signal_url not in signal_map:
            signal_map[signal_url] = signal

    assessments: list[FacultyCandidateAssessment] = []
    for link in links:
        signal = signal_map.get(link)
        assessment = _assess_faculty_candidate(
            link,
            anchor_text=str(getattr(signal, "anchor_text", "") or ""),
            heading_text=str(getattr(signal, "heading_text", "") or ""),
            parent_tags_or_classes=tuple(getattr(signal, "parent_tags_or_classes", ()) or ()),
            link_order=int(getattr(signal, "link_order", 0) or 0),
        )
        assessments.append(assessment)

    type_priority = {
        FACULTY_PAGE_TYPE_FULL: 0,
        FACULTY_PAGE_TYPE_CATEGORY: 1,
        FACULTY_PAGE_TYPE_ELITE: 2,
        FACULTY_PAGE_TYPE_UNKNOWN: 3,
        FACULTY_PAGE_TYPE_NOISE: 4,
    }
    return sorted(
        assessments,
        key=lambda item: (
            -item.score,
            type_priority.get(item.page_type, 9),
            item.url.lower(),
            item.link_order,
        ),
    )


def _select_balanced_faculty_candidates(
    assessments: list[FacultyCandidateAssessment],
    *,
    limit: int = 4,
) -> list[FacultyCandidateAssessment]:
    if not assessments or limit <= 0:
        return []

    accepted = [
        item
        for item in assessments
        if not item.hard_reject
        and item.page_type != FACULTY_PAGE_TYPE_NOISE
        and item.score >= 3
    ]
    if not accepted:
        return []

    full = [item for item in accepted if item.page_type == FACULTY_PAGE_TYPE_FULL]
    category = [item for item in accepted if item.page_type == FACULTY_PAGE_TYPE_CATEGORY]
    elite = [item for item in accepted if item.page_type == FACULTY_PAGE_TYPE_ELITE]
    unknown = [item for item in accepted if item.page_type == FACULTY_PAGE_TYPE_UNKNOWN]

    selected: list[FacultyCandidateAssessment] = []
    selected.extend(full[:2])
    selected.extend(category[:2])
    if not full and not category:
        selected.extend(elite[:1])
    selected.extend(unknown)

    deduped: list[FacultyCandidateAssessment] = []
    seen: set[str] = set()
    for item in selected:
        if item.url in seen:
            continue
        seen.add(item.url)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def _looks_like_retired_url(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in _RETIRED_URL_HINTS) or any(token in url for token in _RETIRED_TEXT_HINTS)


def _looks_like_retired_content(text: str, title_or_url: str = "") -> bool:
    scope = (title_or_url or "").lower()
    if any(token.lower() in scope for token in _RETIRED_TEXT_HINTS):
        return True

    snippet = (text or "")[:12000].lower()
    if not snippet:
        return False

    # Focus on heading/intro text first. Mixed pages often contain both active and retired tabs.
    lines = [line.strip().lower() for line in snippet.splitlines() if line.strip()]
    head = "\n".join(lines[:40])

    retired_head_hits = _count_hint_hits(head, _RETIRED_TEXT_HINTS)
    active_head_hits = _count_hint_hits(head, _ACTIVE_TEXT_HINTS)
    if retired_head_hits > 0 and active_head_hits == 0:
        return True

    retired_total_hits = _count_hint_hits(snippet, _RETIRED_TEXT_HINTS)
    active_total_hits = _count_hint_hits(snippet, _ACTIVE_TEXT_HINTS)

    # Require strong retired signal and no active signal to avoid false positives.
    return retired_total_hits >= 3 and active_total_hits == 0


def _count_hint_hits(text: str, hints: tuple[str, ...]) -> int:
    if not text:
        return 0
    total = 0
    for hint in hints:
        token = hint.lower()
        if token and token in text:
            total += text.count(token)
    return total


def _rank_org_unit_page_candidates(links: list[str], start_url: str) -> list[str]:
    if not links:
        return []

    start_host = (urlparse(start_url).hostname or "").lower()
    strong_tokens = (
        "/jgsz",
        "/yxsz",
        "/zzjg",
        "/jxjg",
        "/xysz",
        "/xybm",
        "/jxkydw",
        "_yjjg",
        "jgsz",
        "yxsz",
        "zzjg",
        "xybm",
        "jxkydw",
        "yjjg",
    )
    medium_tokens = (
        "college",
        "school",
        "department",
        "academy",
        "/xy/",
        "/yx/",
        "xueyuan",
        "yuanxi",
    )
    weak_tokens = (
        "/xygk",
        "/xxgk",
        "/xxgk/xxjj",
        "/xxgk/xxls",
        "/xxgk/lrld",
        "/xxgk/xrld",
        "/about",
        "/overview",
        "/intro",
        "/history",
        "/leader",
        "/news",
        "/notice",
    )

    def _score(link: str) -> int:
        lowered = link.lower()
        parsed = urlparse(link)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lower()
        score = 0
        if host and host != start_host:
            score += 2
        if host.startswith("xxgk") or host.startswith("news") or host.startswith("www2"):
            score -= 3
        if any(token in lowered for token in strong_tokens):
            score += 8
        if any(token in lowered for token in medium_tokens):
            score += 3
        if any(token in lowered for token in weak_tokens):
            score -= 4
        if _is_faculty_platform(link):
            score -= 10
        depth = max(0, path.count("/") - 1)
        if depth <= 2:
            score += 1
        return score

    ranked = sorted(links, key=lambda link: (-_score(link), -len(link), link.lower()))
    deduped: list[str] = []
    seen: set[str] = set()
    for link in ranked:
        if link in seen:
            continue
        seen.add(link)
        deduped.append(link)
    return deduped


def _is_core_academic_kind(kind: str | None) -> bool:
    value = (kind or "").strip().lower()
    if not value:
        return False
    core_tokens = ("college", "school", "department", "academy", "faculty", "xueyuan", "yuanxi")
    research_tokens = ("research", "institute", "lab", "center", "platform")
    if any(token in value for token in research_tokens):
        return False
    return any(token in value for token in core_tokens)


def _ascii_terms(value: str) -> set[str]:
    return {term for term in re.split(r"[^a-z0-9]+", value.lower()) if term}


def _matches_focus_bucket(
    *,
    text: str,
    host_labels: set[str],
    ascii_terms: set[str],
    hint_tokens: tuple[str, ...],
    host_tokens: set[str],
    ascii_tokens: set[str],
) -> bool:
    if host_labels & host_tokens:
        return True
    if ascii_terms & ascii_tokens:
        return True
    return any(token in text for token in hint_tokens)


def _org_unit_focus_rank(unit: OrgUnit) -> int:
    name = (getattr(unit, "name", "") or "").strip().lower()
    kind = (getattr(unit, "kind", "") or "").strip().lower()
    parsed = urlparse((getattr(unit, "url", "") or "").strip().lower())
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    text = f"{name} {kind} {host} {path}"

    host_labels = {label for label in re.split(r"[.\-]+", host) if label}
    ascii_terms = _ascii_terms(f"{name} {kind} {path}")

    if _matches_focus_bucket(
        text=text,
        host_labels=host_labels,
        ascii_terms=ascii_terms,
        hint_tokens=_FOCUS_COMPUTER_HINTS,
        host_tokens=_FOCUS_COMPUTER_HOST_LABELS,
        ascii_tokens=_FOCUS_COMPUTER_ASCII_TERMS,
    ):
        return 0
    if _matches_focus_bucket(
        text=text,
        host_labels=host_labels,
        ascii_terms=ascii_terms,
        hint_tokens=_FOCUS_SOFTWARE_HINTS,
        host_tokens=_FOCUS_SOFTWARE_HOST_LABELS,
        ascii_tokens=_FOCUS_SOFTWARE_ASCII_TERMS,
    ):
        return 1
    if _matches_focus_bucket(
        text=text,
        host_labels=host_labels,
        ascii_terms=ascii_terms,
        hint_tokens=_FOCUS_AI_HINTS,
        host_tokens=_FOCUS_AI_HOST_LABELS,
        ascii_tokens=_FOCUS_AI_ASCII_TERMS,
    ):
        return 2
    if _matches_focus_bucket(
        text=text,
        host_labels=host_labels,
        ascii_terms=ascii_terms,
        hint_tokens=_FOCUS_ELECTRONICS_HINTS,
        host_tokens=_FOCUS_ELECTRONICS_HOST_LABELS,
        ascii_tokens=_FOCUS_ELECTRONICS_ASCII_TERMS,
    ):
        return 3
    return 4

def _org_unit_faculty_priority(unit: OrgUnit, start_host: str) -> tuple[int, int, int, int, int]:
    kind = (getattr(unit, "kind", "") or "").strip().lower()
    parsed = urlparse(getattr(unit, "url", "") or "")
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()

    focus_rank = _org_unit_focus_rank(unit)
    core_rank = 0 if _is_core_academic_kind(kind) else 1
    host_rank = 0 if host != start_host else 1
    detail_rank = 1 if ("/info/" in path or "/news/" in path or "/notice/" in path) else 0
    path_depth = max(0, path.count("/") - 1)
    return (focus_rank, core_rank, detail_rank, host_rank, path_depth)


def _is_faculty_platform(url: str) -> bool:
    """Return True if URL belongs to a shared faculty/teacher platform subdomain."""
    host = (urlparse(url).hostname or "").lower()
    first = host.split(".")[0] if host else ""
    return first.startswith("faculty") or first.startswith("teacher")


def _allow_faculty_candidate_for_org_unit(url: str, *, org_unit_url: str, start_url: str) -> bool:
    """
    Strict host gate for faculty discovery:
    - Allow org-unit host itself.
    - Allow start_host only for explicit faculty directory paths.
    - Reject sibling subdomains and shared teacher/faculty platforms.
    """
    candidate_host = (urlparse(url).hostname or "").lower()
    org_host = (urlparse(org_unit_url).hostname or "").lower()
    start_host = (urlparse(start_url).hostname or "").lower()
    if not candidate_host:
        return False
    if _is_faculty_platform(url):
        return False
    if candidate_host == org_host:
        return True
    if candidate_host == start_host and _is_explicit_faculty_directory_url(url):
        return True
    return False


def _allow_faculty_candidate_for_host_set(url: str, *, start_url: str, org_unit_hosts: set[str]) -> bool:
    """
    Strict host gate when no single org unit is bound (e.g. search fallback):
    - Allow any known org-unit host.
    - Allow start_host only for explicit faculty directory paths.
    - Reject sibling subdomains and shared teacher/faculty platforms.
    """
    candidate_host = (urlparse(url).hostname or "").lower()
    start_host = (urlparse(start_url).hostname or "").lower()
    if not candidate_host:
        return False
    if _is_faculty_platform(url):
        return False
    if candidate_host in org_unit_hosts:
        return True
    if candidate_host == start_host and _is_explicit_faculty_directory_url(url):
        return True
    return False


def _is_college_subdomain(url: str, start_url: str) -> bool:
    """Return True if URL is on a subdomain of the university (not www, not faculty platform)."""
    host = (urlparse(url).hostname or "").lower()
    base_host = (urlparse(start_url).hostname or "").lower()
    if host == base_host:
        return False
    if _is_faculty_platform(url):
        return False
    from agents.crawler.fetchers import _site_root

    return _site_root(host) == _site_root(base_host)


def _same_site(url: str, base_url: str) -> bool:
    return bool(Fetcher.filter_same_domain([url], base_url))


def _url_found_on_page(url: str, page_links: set[str], page_text_lower: str) -> bool:
    """Return True if *url* (or its key components) appears in the page links or text."""
    if url in page_links:
        return True
    # Check if any page link shares the same hostname+path.
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/").lower()
    for link in page_links:
        lp = urlparse(link)
        if (lp.hostname or "").lower() == host and lp.path.rstrip("/").lower() == path:
            return True
    # Fallback: check if the hostname appears in the page text (covers cases
    # where the URL is rendered as text but not as an <a> tag).
    if host and host in page_text_lower:
        return True
    return False


def _is_category_name(name: str) -> bool:
    """Return True if *name* looks like a section heading rather than a specific org unit."""
    return any(pattern in name for pattern in _CATEGORY_PATTERNS)


def _sanitize_url(url: str) -> str:
    """Remove markdown formatting artifacts from LLM-returned URLs."""
    url = url.strip().strip("`").strip("*").strip("_").strip("<").strip(">").strip('"').strip("'")
    while url and url[-1] in ("`", "*", "_", ")", "]", ">", "'", '"'):
        url = url[:-1]
    return url


def _truncate_middle(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    half = max(1, max_chars // 2)
    head = text[:half].rstrip()
    tail = text[-half:].lstrip()
    return head + "\n\n...[truncated]...\n\n" + tail


def _is_pagination_link(url: str) -> bool:
    return bool(_PAGINATION_RE.search(url))


def _is_query_profile_detail_url(url: str) -> bool:
    """Return True for CMS detail URLs whose identity lives in query params."""
    parsed = urlparse((url or "").strip())
    if not parsed.query:
        return False
    params: dict[str, str] = {}
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        params[key.lower()] = value.strip()
    action = params.get("action", "").lower()
    return action == "detailteam" and bool(params.get("uuinid"))


def _extract_urls_from_text(text: str) -> list[str]:
    """Extract HTTP(S) URLs from free-form text (e.g. skills or search snippets)."""
    seen: set[str] = set()
    urls: list[str] = []
    for match in _URL_RE.findall(text):
        url = _sanitize_url(match.rstrip(".,;:)"))
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _dedupe_query_terms(query: str) -> str:
    terms = [term for term in query.split() if term]
    if not terms:
        return ""
    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(term)
    return " ".join(deduped)




