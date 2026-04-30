from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from agents.crawler.fetchers import Fetcher
from agents.crawler.models import OrgUnit
ORG_UNIT_PAGE_KEYWORDS = (
    "college",
    "school",
    "department",
    "academy",
    "faculty",
    "institute",
    "yuan",
    "xueyuan",
    "yuanxi",
    "/jgsz",
    "/zzjg",
    "/jxjg",
    "/yxsz",
    "/xysz",
    "/xybm",
    "/jxkydw",
    "jxkydw",
    "yjjg",
    "/xygk",
    "/xxgk",
    # Full pinyin forms used by some universities (e.g. RUC zuzhijigou.html)
    "zuzhijigou",
    "jiaoxuejigou",
    "jiaoxuedanwei",
    "yuanxishezhi",
    "xueyuanshezhi",
    # Chinese keywords (multi-char only to avoid false positives)
    "学院",
    "院系",
    "组织机构",
    "机构设置",
    "院系设置",
    "学院设置",
    "教学单位",
    "科研机构",
)

FACULTY_KEYWORDS = (
    "teacher",
    "faculty",
    "staff",
    "people",
    "team",
    "tutor",
    "supervisor",
    "professor",
    "mentor",
    "directory",
    "list",
    "szdw",
    "szll",
    "jsdw",
    "qzjs",
    "rcdw",
    "jzg",
    "faculty_list",
    "teacher_list",
    "facultylist",
    "teacherlist",
    "teaching_staff",
    "师资",
    "教师",
    "导师",
    "教工",
    "人才",
)
def _keyword_filter(links: list[str], keywords: tuple[str, ...]) -> list[str]:
    lowered = [(keyword, keyword.lower()) for keyword in keywords]
    return [
        link
        for link in links
        if any(keyword in link or ascii_keyword in link.lower() for keyword, ascii_keyword in lowered)
    ]


def _looks_like_faculty_page(url: str) -> bool:
    return bool(_keyword_filter([url], FACULTY_KEYWORDS))


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

    ranked = sorted(links, key=lambda link: (_score(link), -len(link)), reverse=True)
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

    ranked = sorted(links, key=lambda link: (_score(link), -len(link)), reverse=True)
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


def _org_unit_faculty_priority(unit: OrgUnit, start_host: str) -> tuple[int, int, int, int]:
    kind = (unit.kind or "").strip().lower()
    parsed = urlparse(unit.url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()

    core_rank = 0 if _is_core_academic_kind(kind) else 1
    host_rank = 0 if host != start_host else 1
    detail_rank = 1 if ("/info/" in path or "/news/" in path or "/notice/" in path) else 0
    path_depth = max(0, path.count("/") - 1)
    return (core_rank, detail_rank, host_rank, path_depth)


def _is_faculty_platform(url: str) -> bool:
    """Return True if URL belongs to a faculty.xxx.edu.cn homepage platform (not a real faculty list)."""
    host = (urlparse(url).hostname or "").lower()
    first = host.split(".")[0] if host else ""
    return first.startswith("faculty")


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


_COMMON_FACULTY_PATHS = (
    "/szdw/szll.htm",
    "/szdw.htm",
    "/szdw/",
    "/szll.htm",
    "/szll/",
    "/rcpy/szdw.htm",
    "/sz/szdw.htm",
    "/teacher/",
    "/teachers/",
    "/faculty/",
    "/people/",
    "/szrc.htm",
    "/szdw/jsdw.htm",
    "/szdw/qzjs.htm",
    "/szdw/index.htm",
    "/yjdw/szdw.htm",
    "/jszy/",
    "/rydw/",
)

_INTERMEDIATE_ORG_PATHS = (
    "/zuzhijigou.html",
    "/jgsz.htm",
    "/jgsz/",
    "/jgsz/jxkyjg.htm",
    "/yxsz.htm",
    "/yxsz/",
    "/zzjg.htm",
    "/zzjg/",
    "/jxjg.htm",
    "/jxjg/",
    "/xysz.htm",
    "/xysz/",
)
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


_CATEGORY_PATTERNS = (
    "教学科研",
    "科研机构",
    "研究机构",
    "教学单位",
    "直属单位",
    "附属单位",
    "独立学院",
    "党群部门",
    "行政部门",
    "管理机构",
    "教辅机构",
    "群团组织",
)


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


_PAGINATION_RE = re.compile(
    r"[?&](page|p|pagenum|pn|start|offset)=\d+"
    r"|/list_\d+\.htm"
    r"|/index_\d+\.htm"
    r"|/page/\d+"
    r"|-\d+\.htm$",
    re.IGNORECASE,
)


def _is_pagination_link(url: str) -> bool:
    return bool(_PAGINATION_RE.search(url))


_URL_RE = re.compile(r"https?://[^\s\)\]\"'>]+")


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

