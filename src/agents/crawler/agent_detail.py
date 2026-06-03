from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import or_, select

from agents.crawler.models import Professor
from agents.crawler.url_heuristics import (
    _is_explicit_faculty_directory_url,
    _is_faculty_platform,
    _is_non_faculty_noise_url,
    _is_pagination_link,
    _looks_like_retired_content,
    _looks_like_retired_url,
    _sanitize_url,
)

_FACULTY_CATEGORY_STEMS = frozenset(
    {
        "bsh",
        "bsds",
        "bssds",
        "byds",
        "cxzx",
        "fjs",
        "gjjcqnjjhdz",
        "ggjs",
        "gxnjszx",
        "jcjs",
        "js",
        "js1",
        "jsjgcx",
        "jsjkxx",
        "jsgg",
        "jjzx",
        "jzg",
        "msfc",
        "qzjs",
        "qtjs",
        "rcyj",
        "rgznx",
        "rjgcx",
        "rsgz",
        "shidao",
        "ssds",
        "sys",
        "szdw",
        "szll",
        "txtxyrjgcs",
        "tutor",
        "yxqnjjhdz",
        "yjry",
        "ys",
        "yjsds",
        "zzjs",
    }
)

_DETAIL_URL_HINTS = (
    "/info/",
    "/teacher/",
    "/teachers/",
    "/people/",
    "/show",
    "/detail",
    "/profile",
    "/mentor",
    "teachershow",
    "teachershouw",
    "teacher_show",
    "showteacher",
)

_CLEAR_PROFILE_DETAIL_HINTS = (
    "/info/",
    "/show",
    "/detail",
    "/profile",
    "/mentor",
    "teachershow",
    "teachershouw",
    "teacher_show",
    "showteacher",
)

_PROFILE_EVIDENCE_TOKENS = (
    "@",
    "email",
    "e-mail",
    "邮箱",
    "电子邮件",
    "电话",
    "联系方式",
    "办公电话",
    "研究方向",
    "研究领域",
    "科研方向",
    "个人简介",
    "教育经历",
    "代表论文",
    "科研项目",
    "homepage",
    "个人主页",
)

_PERSON_ANCHOR_BLOCKLIST = (
    "师资",
    "教师",
    "队伍",
    "名录",
    "列表",
    "中心",
    "学院",
    "系",
    "团队",
    "栏目",
    "更多",
    "查看",
    "详情",
    "首页",
    "院士",
    "杰出",
    "青年",
    "基金",
    "人才",
    "招聘",
    "人事",
    "政策",
    "通知",
    "公告",
    "news",
    "notice",
    "list",
    "more",
)


class DetailEnricher:
    """Manages detail profile candidate filtering and processing for an agent."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    async def enrich_profiles_with_detail_backend(
        self,
        current: Any,
        fetched: Any,
        skills: str,
        reserved_urls: set[str] | None = None,
    ) -> None:
        await enrich_profiles_with_detail_backend(self.agent, current, fetched, skills, reserved_urls=reserved_urls)

    async def enrich_profiles_with_human(
        self,
        current: Any,
        fetched: Any,
        skills: str,
        reserved_urls: set[str] | None = None,
    ) -> None:
        await enrich_profiles_with_human(self.agent, current, fetched, skills, reserved_urls=reserved_urls)

    async def process_detail_urls_with_human(self, urls: list[str], current: Any, skills: str) -> None:
        await process_detail_urls_with_human(self.agent, urls, current, skills)

    def extract_detail_profile_links(
        self,
        links: list[str],
        current_url: str,
        link_signals: tuple[Any, ...] | list[Any] | None = None,
    ) -> list[str]:
        return extract_detail_profile_links(self.agent, links, current_url, link_signals=link_signals)

    def detail_org_unit_key(self, current: Any) -> str:
        return detail_org_unit_key(self.agent, current)

    @staticmethod
    def derive_section_prefix(path: str) -> str:
        return derive_section_prefix(path)

    def is_failed_detail_fetch(self, fetched: Any) -> bool:
        return is_failed_detail_fetch(self.agent, fetched)

    def is_retired_page(self, fetched: Any) -> bool:
        return is_retired_page(self.agent, fetched)

    def looks_like_detail_directory_page(self, fetched: Any) -> bool:
        return looks_like_detail_directory_page(self.agent, fetched)


def _url_path_stem(url: str) -> str:
    leaf = urlparse(url).path.lower().rstrip("/").rsplit("/", 1)[-1]
    return leaf.rsplit(".", 1)[0] if "." in leaf else leaf


def _is_faculty_directory_or_category_link(url: str) -> bool:
    lowered = (url or "").lower()
    parsed = urlparse(lowered)
    path = parsed.path
    stem = _url_path_stem(lowered)
    if not stem:
        return False
    if _is_pagination_link(lowered):
        return True
    if re.fullmatch(r"\d+", stem):
        return True
    if stem in _FACULTY_CATEGORY_STEMS:
        return True
    if "tu-list" in stem or stem in {"list", "teacher_list", "faculty_list"}:
        return True
    if any(token in path for token in ("/szdw/", "/jsdw/", "/szll/", "/team/", "/staff/")):
        return stem.endswith(("list", "index")) or stem in _FACULTY_CATEGORY_STEMS
    return False


def _looks_like_profile_detail_url(url: str) -> bool:
    lowered = (url or "").lower()
    if any(token in lowered for token in _CLEAR_PROFILE_DETAIL_HINTS):
        return True
    path = urlparse(lowered).path
    return bool(re.search(r"/info/\d+/\d+(\.s?html?)?$", path))


async def enrich_profiles_with_detail_backend(
    self: Any,
    current: Any,
    fetched: Any,
    skills: str,
    *,
    reserved_urls: set[str] | None = None,
) -> None:
    if not self._is_interactive or not self.detail_enrich_enabled:
        return
    await self._enrich_profiles_with_human(current, fetched, skills, reserved_urls=reserved_urls)


async def enrich_profiles_with_human(
    self: Any,
    current: Any,
    fetched: Any,
    skills: str,
    *,
    reserved_urls: set[str] | None = None,
) -> None:
    org_unit_key = self._detail_org_unit_key(current)
    processed = self._detail_processed_by_org_unit.get(org_unit_key, 0)
    remaining = self.detail_profile_hard_cap_per_org_unit - processed
    if remaining <= 0:
        self.logger.debug(
            "Detail enrichment cap reached org_unit=%s cap=%s",
            current.label or "Unknown",
            self.detail_profile_hard_cap_per_org_unit,
        )
        return

    candidates = self._extract_detail_profile_links(
        fetched.links,
        fetched.url,
        link_signals=getattr(fetched, "link_signals", ()) or (),
    )
    if not candidates:
        return

    reserved = {_sanitize_url(url) for url in (reserved_urls or set()) if _sanitize_url(url)}
    enriched_names = await _load_enriched_names(self, current, org_unit_key)
    sig_by_url = {
        getattr(sig, "url", ""): sig
        for sig in (getattr(fetched, "link_signals", ()) or ())
        if getattr(sig, "url", None)
    }

    pending: list[str] = []
    skipped_by_name = 0
    skipped_reserved = 0
    for link in candidates:
        if len(pending) >= remaining:
            break
        normalized = _sanitize_url(link)
        if normalized in reserved:
            skipped_reserved += 1
            continue
        if link in self._detail_visited_urls or link in self.visited_urls:
            continue
        if enriched_names and _anchor_matches_enriched_name(sig_by_url.get(link), enriched_names):
            skipped_by_name += 1
            continue
        self._detail_visited_urls.add(link)
        pending.append(link)

    if skipped_by_name:
        self._pipeline_stats["detail_links_dropped_already_enriched"] = int(
            self._pipeline_stats.get("detail_links_dropped_already_enriched", 0)
        ) + skipped_by_name
        self.logger.debug(
            "Detail enrichment skipped %s links whose anchor matched already-enriched professors org_unit=%s page=%s",
            skipped_by_name,
            current.label or "Unknown",
            fetched.url,
        )
    if skipped_reserved:
        self._pipeline_stats["detail_links_reserved_for_list"] = int(
            self._pipeline_stats.get("detail_links_reserved_for_list", 0)
        ) + skipped_reserved
        self.logger.debug(
            "Detail enrichment reserved %s links for list/followup processing org_unit=%s page=%s",
            skipped_reserved,
            current.label or "Unknown",
            fetched.url,
        )

    if not pending:
        if candidates and skipped_reserved < len(candidates):
            self._pipeline_stats["detail_pending_empty_with_candidates"] = int(
                self._pipeline_stats.get("detail_pending_empty_with_candidates", 0)
            ) + 1
            sample_visited = [c for c in candidates if c in self._detail_visited_urls or c in self.visited_urls][:3]
            self.logger.warning(
                "Detail enrichment found %s candidates but produced 0 pending org_unit=%s page=%s "
                "(all already visited or matched enriched names; sample=%s skipped_by_name=%s)",
                len(candidates),
                current.label or "Unknown",
                fetched.url,
                sample_visited,
                skipped_by_name,
            )
        return
    self._detail_processed_by_org_unit[org_unit_key] = processed + len(pending)
    await self._process_detail_urls_with_human(pending, current, skills)
    # Refresh enriched-name cache so subsequent list pages benefit from
    # whatever detail extraction just succeeded.
    self._enriched_names_by_org_unit.pop(org_unit_key, None)


async def process_detail_urls_with_human(self: Any, urls: list[str], current: Any, skills: str) -> None:
    next_depth = current.depth + 1
    if not self._within_depth(next_depth):
        return
    for url in urls:
        if url in self.visited_urls:
            continue
        fetched = await self._fetch_url(url, next_depth)
        if fetched is None:
            continue
        if self._is_retired_page(fetched):
            self.logger.info("Skip retired human detail page url=%s", fetched.url)
            continue
        if self._looks_like_detail_directory_page(fetched):
            self._pipeline_stats["detail_directory_skipped"] = int(
                self._pipeline_stats.get("detail_directory_skipped", 0)
            ) + 1
            self.logger.debug("Skip directory/list page from detail enrichment url=%s", fetched.url)
            continue
        llm_queue = getattr(self, "_active_detail_llm_queue", None)
        if llm_queue is not None and getattr(self, "pipeline_enabled", False):
            await self._enqueue_extraction_task(
                current,
                fetched,
                llm_queue=llm_queue,
                detail_mode=True,
                priority=1,
            )
            continue
        await self._extract_professors_from_page(
            current,
            fetched,
            skills,
            detail_mode=True,
        )


def extract_detail_profile_links(
    self: Any,
    links: list[str],
    current_url: str,
    *,
    link_signals: tuple[Any, ...] | list[Any] | None = None,
) -> list[str]:
    same_domain = self.fetcher.filter_same_domain(links, self.start_url)
    current_parsed = urlparse(current_url)
    current_host = (current_parsed.hostname or "").lower()
    current_path = current_parsed.path.lower()
    current_dir = self._derive_section_prefix(current_path)
    current_is_noise = _is_non_faculty_noise_url(current_url)
    signal_by_url = {
        _sanitize_url(getattr(sig, "url", "")): sig
        for sig in (link_signals or ())
        if _sanitize_url(getattr(sig, "url", ""))
    }

    detail_hints = _DETAIL_URL_HINTS
    section_hints = ("/szdw/", "/team/", "/staff/", "/jsdw/")
    noise_hints = (
        "/gywm/",
        "/djgz/",
        "/rcpy/",
        "/pxfz/",
        "/kxyj/",
        "/download/",
        "/about/",
        "/intro/",
        "/history/",
        "/leader/",
        "/lxdh/",
        "/index",
    )
    file_ext_hints = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar")

    dropped_noise = 0
    dropped_parent_noise = 0
    dropped_directory = 0
    candidates: list[str] = []
    for link in same_domain:
        if link == current_url:
            continue
        if _is_faculty_platform(link) or _is_pagination_link(link):
            if _is_pagination_link(link):
                dropped_directory += 1
            continue
        if _looks_like_retired_url(link):
            continue
        if _is_non_faculty_noise_url(link):
            dropped_noise += 1
            continue
        parsed = urlparse(link)
        host = (parsed.hostname or "").lower()
        if current_host and host != current_host:
            continue
        lowered = link.lower()
        if any(token in lowered for token in noise_hints):
            continue
        if any(lowered.endswith(ext) for ext in file_ext_hints):
            continue
        path = parsed.path.lower()
        looks_like_profile_detail = _looks_like_profile_detail_url(link)
        anchor_looks_personal = _link_signal_looks_like_person(signal_by_url.get(_sanitize_url(link)))
        if _is_faculty_directory_or_category_link(link) and not looks_like_profile_detail:
            dropped_directory += 1
            continue
        related_by_path = False
        if current_dir:
            prefix = current_dir.rstrip("/")
            related_by_path = bool(prefix and path.startswith(prefix + "/"))
        related_by_hint = any(token in lowered for token in detail_hints)
        # If current page is noise, avoid same-directory fan-out unless target is explicit faculty directory.
        if current_is_noise and related_by_path and not _is_explicit_faculty_directory_url(link):
            dropped_parent_noise += 1
            continue
        if not related_by_path and not related_by_hint:
            continue
        if not (looks_like_profile_detail or anchor_looks_personal):
            dropped_directory += 1
            continue
        candidates.append(link)

    if dropped_noise:
        self._pipeline_stats["detail_links_dropped_noise"] = int(self._pipeline_stats.get("detail_links_dropped_noise", 0)) + dropped_noise
    if dropped_parent_noise:
        self._pipeline_stats["detail_links_dropped_noise"] = int(self._pipeline_stats.get("detail_links_dropped_noise", 0)) + dropped_parent_noise
    if dropped_directory:
        self._pipeline_stats["detail_links_dropped_directory"] = int(
            self._pipeline_stats.get("detail_links_dropped_directory", 0)
        ) + dropped_directory
    if dropped_noise or dropped_parent_noise or dropped_directory:
        self.logger.debug(
            "Detail links filtered current=%s kept=%s dropped_noise=%s dropped_parent_noise=%s dropped_directory=%s drop_reason=url_noise_token",
            current_url,
            len(candidates),
            dropped_noise,
            dropped_parent_noise,
            dropped_directory,
        )

    def _score(url: str) -> tuple[int, int]:
        lowered = url.lower()
        signal = signal_by_url.get(_sanitize_url(url))
        depth = max(0, urlparse(url).path.count("/") - 1)
        score = depth
        if current_dir and urlparse(url).path.lower().startswith(current_dir.rstrip("/") + "/"):
            score += 4
        if any(token in lowered for token in detail_hints):
            score += 4
        if _link_signal_looks_like_person(signal):
            score += 3
        if any(token in lowered for token in section_hints):
            score += 2
        if any(token in lowered for token in noise_hints):
            score -= 6
        return score, -len(url)

    ranked = sorted(candidates, key=_score, reverse=True)
    deduped: list[str] = []
    seen: set[str] = set()
    for link in ranked:
        if link in seen:
            continue
        if _score(link)[0] < 3:
            continue
        seen.add(link)
        deduped.append(link)
    return deduped


def detail_org_unit_key(self: Any, current: Any) -> str:
    if current.org_unit_id is not None:
        return f"id:{current.org_unit_id}"
    label = (current.label or "").strip().lower()
    if label:
        return f"label:{label}"
    return f"url:{_sanitize_url(current.url)}"


def derive_section_prefix(path: str) -> str:
    normalized = (path or "").strip().lower()
    if not normalized:
        return ""
    parent, _, leaf = normalized.rpartition("/")
    if leaf.endswith((".htm", ".html", ".shtml")):
        stem = leaf.rsplit(".", 1)[0]
        if stem:
            return f"{parent}/{stem}" if parent else f"/{stem}"
    if parent:
        return parent
    return normalized


def is_failed_detail_fetch(self: Any, fetched: Any) -> bool:
    if fetched.block_reason:
        return True
    if fetched.status_code in {0, 202, 429, 503}:
        return True
    if fetched.status_code >= 400:
        return True
    if len((fetched.text or "").strip()) < 160 and len(fetched.links) < 2:
        return True
    return False


def is_retired_page(self: Any, fetched: Any) -> bool:
    if _looks_like_retired_url(fetched.url):
        return True
    return _looks_like_retired_content(fetched.text, fetched.url)


def looks_like_detail_directory_page(self: Any, fetched: Any) -> bool:
    url = getattr(fetched, "url", "") or ""
    if _looks_like_profile_detail_url(url):
        return False
    text = (getattr(fetched, "text", "") or "").lower()
    if any(token in text for token in _PROFILE_EVIDENCE_TOKENS):
        return False
    links = list(getattr(fetched, "links", ()) or ())
    if _is_faculty_directory_or_category_link(url):
        return True
    if len(links) >= 1 and any(_is_faculty_directory_or_category_link(link) for link in links):
        return True
    return False


_NAME_NOISE_TOKENS = (
    "副教授",
    "副研究员",
    "助理研究员",
    "助理教授",
    "院士",
    "杰出教授",
    "教授",
    "讲师",
    "研究员",
    "导师",
    "博导",
    "硕导",
    "professor",
    "associate",
    "assistant",
    "lecturer",
    "researcher",
)


def _normalize_anchor_for_name_match(text: str) -> str:
    if not text:
        return ""
    cleaned = text.strip().lower()
    if not cleaned:
        return ""
    for token in _NAME_NOISE_TOKENS:
        cleaned = cleaned.replace(token, " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _link_signal_looks_like_person(signal: Any) -> bool:
    if signal is None:
        return False
    pieces: list[str] = []
    for attr in ("anchor_text", "heading_text"):
        value = getattr(signal, attr, "")
        if value:
            pieces.append(str(value))
    if not pieces:
        return False
    raw = " ".join(pieces).strip().lower()
    cleaned = _normalize_anchor_for_name_match(raw)
    if not cleaned:
        return False
    compact = re.sub(r"[\s·•\-_/|:：,，.。()（）\[\]【】]+", "", cleaned)
    if not compact:
        return False
    if any(token in compact for token in _PERSON_ANCHOR_BLOCKLIST):
        return False
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", compact)
    if 2 <= len(cjk_chars) <= 4 and len(compact) <= 6:
        return True
    words = re.findall(r"[a-z][a-z'.-]+", cleaned)
    if 2 <= len(words) <= 4 and len("".join(words)) >= 4:
        return True
    return False


def _anchor_matches_enriched_name(signal: Any, enriched_names: set[str]) -> bool:
    """Return True when a link's anchor / heading text references an already-
    enriched professor in this org unit. Used to suppress duplicate detail
    fetches for the same person across multiple list pages."""
    if not enriched_names:
        return False
    pieces: list[str] = []
    for attr in ("anchor_text", "heading_text"):
        value = getattr(signal, attr, "") if signal is not None else ""
        if value:
            pieces.append(str(value))
    if not pieces:
        return False
    haystack = _normalize_anchor_for_name_match(" ".join(pieces))
    if not haystack or len(haystack) < 2:
        return False
    for name in enriched_names:
        candidate = (name or "").strip().lower()
        if not candidate or len(candidate) < 2:
            continue
        if candidate in haystack:
            return True
    return False


async def _load_enriched_names(self: Any, current: Any, org_unit_key: str) -> set[str]:
    cached = self._enriched_names_by_org_unit.get(org_unit_key)
    if cached is not None:
        return cached
    org_unit_name = (getattr(current, "label", "") or "").strip()
    if not org_unit_name:
        self._enriched_names_by_org_unit[org_unit_key] = set()
        return set()
    async with self.db.session() as session:
        rows = (
            await session.execute(
                select(Professor.name).where(
                    Professor.org_unit_name == org_unit_name,
                    or_(
                        Professor.research_areas.isnot(None),
                        Professor.bio.isnot(None),
                    ),
                )
            )
        ).scalars().all()
    names = {str(name).strip() for name in rows if str(name or "").strip()}
    self._enriched_names_by_org_unit[org_unit_key] = names
    return names
