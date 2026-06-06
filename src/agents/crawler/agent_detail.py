from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, or_, select

from agents.crawler.models import CrawlTask, CrawlTaskKind, OrgUnit, Professor, ProfessorAffiliation
from agents.crawler.sanitizer import contains_self_academician_hint, normalize_name
from agents.crawler.url_heuristics import (
    _is_explicit_faculty_directory_url,
    _is_faculty_platform,
    _is_non_faculty_noise_url,
    _is_pagination_link,
    _is_query_profile_detail_url,
    _looks_like_retired_content,
    _looks_like_retired_url,
    _same_site,
    _sanitize_url,
)
from agents.crawler.db.professors import normalize_professor_homepage

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
    "个人概况",
    "学习工作经历",
    "工作经历",
    "教育经历",
    "教学情况",
    "管理经验",
    "代表论文",
    "论文著作",
    "科研项目",
    "科研成果",
    "homepage",
    "个人主页",
)

_SNAPSHOT_BIO_HEADINGS = (
    "个人简介",
    "个人概况",
    "简介",
)

_SNAPSHOT_BIO_STOP_TOKENS = (
    "要求",
    "招生要求",
    "部分论文",
    "论文著作",
    "项目成果",
    "获奖荣誉",
    "代表论文",
    "科研项目",
    "科研成果",
    "footLogo",
    "版权所有",
    "如对我研究方向感兴趣",
    "---",
)

_SNAPSHOT_NAVIGATION_BIO_TOKENS = (
    "校园地图",
    "VI系统",
    "校园图库",
    "网上服务大厅",
    "校友邮箱",
    "图书馆",
)

_SNAPSHOT_FIELD_BOUNDARY_RE = re.compile(
    r"(?:^|\s+)(?:姓名|职称|职务|所在系所|电话|办公电话|电子邮箱|邮箱|个人主页|办公地址|"
    r"主要研究方向|研究方向|研究领域|科研方向|e-?mail|email\s+address|mail|phone|tel|telephone|homepage|home\s+page|website)\s*[：:]",
    re.IGNORECASE,
)
_SNAPSHOT_LEADING_FIELD_LABEL_RE = re.compile(
    r"^\s*(?:姓名|职称|职务|所在系所|电话|办公电话|电子邮箱|邮箱|个人主页|办公地址|"
    r"主要研究方向|研究方向|研究领域|科研方向|e-?mail|email\s+address|mail|phone|tel|telephone|homepage|home\s+page|website)\s*[：:]",
    re.IGNORECASE,
)

_SNAPSHOT_EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_SNAPSHOT_URL_RE = re.compile(r"https?://[^\s\])>\"']+")
_SNAPSHOT_PHONE_RE = re.compile(r"(?:\+?\d[\d\-()（） ]{5,}\d)")
_SNAPSHOT_RESEARCH_CONTACT_LABELS = {
    "email",
    "e-mail",
    "mail",
    "emailaddress",
    "邮箱",
    "电子邮箱",
    "电子邮件",
    "联系电话",
    "电话",
    "办公电话",
    "phone",
    "tel",
    "telephone",
    "homepage",
    "home page",
    "website",
    "个人主页",
    "主页",
    "网址",
}

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
    "办事",
    "指南",
    "流程",
    "资料下载",
    "申请表",
    "审批表",
    "办理程序",
    "薪酬福利",
    "通知",
    "公告",
    "news",
    "notice",
    "list",
    "more",
)

_SERVICE_GUIDE_TOKENS = (
    "办事指南",
    "办事流程",
    "资料下载",
    "人事政策",
    "薪酬福利",
    "办理程序",
    "申请表",
    "审批表",
)
_SERVICE_GUIDE_TITLE_RE = re.compile(
    r"(?:^|\n)\s*#{1,4}\s*(?:【[^】]{1,30}】)?[^\n#]{0,120}"
    r"(?:办事指南|办事流程|资料下载|办理程序|申请表|审批表|人事政策|薪酬福利)"
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
    if _is_query_profile_detail_url(lowered):
        return False
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
    if _is_query_profile_detail_url(lowered):
        return True
    if any(token in lowered for token in _CLEAR_PROFILE_DETAIL_HINTS):
        return True
    path = urlparse(lowered).path
    return bool(re.search(r"/info/\d+/\d+(\.s?html?)?$", path))


def extract_detail_profile_record_from_snapshot(text: str, *, page_url: str = "") -> dict[str, Any] | None:
    """Extract a conservative single-profile record from stored detail text."""
    raw = str(text or "")
    if len(raw.strip()) < 120:
        return None
    if page_url and not _looks_like_profile_detail_url(page_url):
        return None

    relevant = _snapshot_relevant_text(raw)
    if _looks_like_service_guide_snapshot(relevant, page_url=page_url):
        return None
    name = _extract_snapshot_name(relevant)
    if not name:
        return None
    if _snapshot_has_multiple_labeled_names(relevant, name):
        return None

    research_areas = _extract_snapshot_research_areas(relevant)
    bio = _extract_snapshot_bio(relevant, name)
    email = _extract_snapshot_email(relevant)
    phone = _extract_snapshot_phone(relevant)
    if not any((research_areas, bio, email, phone)):
        return None

    title = _extract_snapshot_title(relevant, name)
    personal_homepage = _extract_snapshot_personal_homepage(relevant)
    record: dict[str, Any] = {"name": name}
    if title:
        record["title"] = title
    if research_areas:
        record["research_areas"] = research_areas
    if email:
        record["email"] = email
    if phone:
        record["phone"] = phone
    if page_url:
        record["homepage"] = page_url
    elif personal_homepage:
        record["homepage"] = personal_homepage
    if personal_homepage and personal_homepage != record.get("homepage"):
        record["external_link"] = personal_homepage
    if bio:
        record["bio"] = bio

    if _snapshot_self_academician_evidence(name, title, bio, relevant):
        record["is_academician"] = True
        record["_self_academician_evidence"] = True
    return record


def _looks_like_service_guide_snapshot(text: str, *, page_url: str = "") -> bool:
    if page_url and _is_non_faculty_noise_url(page_url):
        return True
    if _SERVICE_GUIDE_TITLE_RE.search(text or ""):
        return True
    token_hits = sum(1 for token in _SERVICE_GUIDE_TOKENS if token in (text or ""))
    if token_hits <= 0:
        return False
    if "当前位置" in text and token_hits >= 1:
        return True
    if "联系人" in text and token_hits >= 2:
        return True
    return False


def _snapshot_relevant_text(text: str) -> str:
    stop_positions = [
        index
        for token in ("footLogo", "版权所有", "四川大学计算机学院版权所有", "邮编：610")
        if (index := text.find(token)) >= 0
    ]
    if stop_positions:
        return text[: min(stop_positions)]
    return text


def _snapshot_has_multiple_labeled_names(text: str, expected: str) -> bool:
    names = {
        _clean_snapshot_name(match.group("value"))
        for match in re.finditer(r"(?:^|\n)\s*姓名\s*[：:]\s*(?P<value>[^\n|]+)", text)
    }
    names.discard("")
    return len({name for name in names if name != expected}) > 0


def _extract_snapshot_name(text: str) -> str | None:
    for pattern in (
        r"(?:^|\n)\s*姓名\s*[：:]\s*(?P<value>[^\n|]+)",
        r"(?:^|\n)\s*#{1,3}\s*(?P<value>[^\n#]+)",
    ):
        for match in re.finditer(pattern, text):
            name = _clean_snapshot_name(match.group("value"))
            if _looks_like_person_name(name):
                return name
    return None


def _clean_snapshot_name(value: str) -> str:
    text = _clean_snapshot_text(value)
    text = re.split(r"\s+(?:职称|职务|电话|电子邮箱|个人主页)\s*[：:]", text, maxsplit=1)[0]
    return normalize_name(text.strip("：:|,，;；。 "))


def _looks_like_person_name(value: str) -> bool:
    text = (value or "").strip()
    if _SNAPSHOT_LEADING_FIELD_LABEL_RE.match(text):
        return False
    if not text or any(token in text for token in _PERSON_ANCHOR_BLOCKLIST):
        return False
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
    if 2 <= len(cjk_chars) <= 4 and len(text) <= 8:
        return True
    words = re.findall(r"[A-Za-z][A-Za-z'.-]+", text)
    return 2 <= len(words) <= 4 and len(" ".join(words)) <= 60


def _extract_snapshot_title(text: str, name: str) -> str | None:
    title = _extract_snapshot_labeled_value(text, ("职称", "职务"), max_chars=80)
    if title:
        return title
    name_index = text.find(name)
    if name_index < 0:
        return None
    window = text[name_index : name_index + 260]
    for token in (
        "院士",
        "副主任医师",
        "主任医师",
        "主治医师",
        "住院医师",
        "副研究员",
        "研究员",
        "副教授",
        "教授",
        "助理教授",
        "讲师",
        "高级工程师",
        "博士后",
    ):
        if token in window:
            return token
    return None


def _extract_snapshot_research_areas(text: str) -> list[str] | None:
    value = _extract_snapshot_labeled_value(text, ("主要研究方向", "研究方向", "研究领域", "科研方向"), max_chars=240)
    if not value:
        return None
    value = _truncate_snapshot_value_at_stop(value)
    terms: list[str] = []
    for part in re.split(r"[；;、，,\n]|和", value):
        term = _clean_snapshot_text(part).strip("：:，,；;。. ")
        if not term or len(term) > 60 or _looks_like_snapshot_research_contact(term):
            continue
        if term not in terms:
            terms.append(term)
    return terms[:8] or None


def _looks_like_snapshot_research_contact(value: str) -> bool:
    text = _clean_snapshot_text(value).strip("：:，,；;。. ")
    if not text:
        return True
    lowered = text.lower()
    compact = re.sub(r"[\s_\-]+", "", lowered)
    if lowered in _SNAPSHOT_RESEARCH_CONTACT_LABELS or compact in _SNAPSHOT_RESEARCH_CONTACT_LABELS:
        return True
    if _SNAPSHOT_EMAIL_RE.search(text) or _SNAPSHOT_URL_RE.search(text):
        return True
    if re.match(r"^(?:e-?mail|mail|email\s+address|邮箱|电子邮箱|电子邮件)\s*[：:]", text, re.IGNORECASE):
        return True
    if re.match(r"^(?:phone|tel|telephone|电话|办公电话|联系电话)\s*[：:]", text, re.IGNORECASE):
        return True
    return bool(_SNAPSHOT_PHONE_RE.fullmatch(text))


def _extract_snapshot_email(text: str) -> str | None:
    emails: list[str] = []
    for match in _SNAPSHOT_EMAIL_RE.finditer(text):
        email = match.group(0).strip().lower()
        if email not in emails:
            emails.append(email)
    return "；".join(emails[:3]) if emails else None


def _extract_snapshot_phone(text: str) -> str | None:
    line_value = _extract_snapshot_labeled_value(text, ("办公电话", "电话"), max_chars=80)
    if not line_value:
        return None
    match = _SNAPSHOT_PHONE_RE.search(line_value)
    return match.group(0).strip() if match else None


def _extract_snapshot_personal_homepage(text: str) -> str | None:
    value = _extract_snapshot_labeled_value(text, ("个人主页", "主页", "Homepage"), max_chars=240)
    if not value:
        return None
    urls = []
    for match in _SNAPSHOT_URL_RE.finditer(value):
        url = match.group(0).strip()
        if url.lower().startswith("mailto:"):
            continue
        if url not in urls:
            urls.append(url)
    return urls[0] if urls else None


def _extract_snapshot_bio(text: str, name: str) -> str | None:
    name_index = text.find(name)
    searchable = text[name_index:] if name_index >= 0 else text
    lines = searchable.splitlines()
    for index, raw_line in enumerate(lines):
        line = _clean_snapshot_text(raw_line)
        if not line:
            continue
        heading = next((item for item in _SNAPSHOT_BIO_HEADINGS if item in line), None)
        if not heading:
            continue
        fragments: list[str] = []
        after_heading = line.split(heading, 1)[1].strip("：: 　")
        after_heading = _truncate_snapshot_value_at_stop(after_heading)
        if after_heading:
            fragments.append(after_heading)
        for next_line in lines[index + 1 :]:
            cleaned = _clean_snapshot_text(next_line)
            if not cleaned:
                continue
            cleaned = _strip_snapshot_bio_heading(cleaned)
            truncated = _truncate_snapshot_value_at_stop(cleaned)
            if truncated:
                fragments.append(truncated)
            if truncated != cleaned:
                break
            if any(token in cleaned for token in _SNAPSHOT_BIO_STOP_TOKENS):
                break
        bio = " ".join(fragments).strip()
        bio = re.sub(r"\s+", " ", bio).strip("：:；;，,。 ")
        if _looks_like_snapshot_navigation_bio(bio):
            return None
        if len(bio) > 900:
            bio = bio[:900].rstrip("，,；;。 ") + "。"
        if bio and name in bio:
            return bio
        if bio and len(bio) >= 20:
            return bio
    return None


def _looks_like_snapshot_navigation_bio(value: str) -> bool:
    text = re.sub(r"\s+", "", str(value or ""))
    if not text or len(text) > 220:
        return False
    hits = sum(1 for token in _SNAPSHOT_NAVIGATION_BIO_TOKENS if token in text)
    return hits >= 3


def _strip_snapshot_bio_heading(value: str) -> str:
    for heading in _SNAPSHOT_BIO_HEADINGS:
        if value.startswith(heading):
            return value.split(heading, 1)[1].strip("：: 　")
    return value


def _extract_snapshot_labeled_value(text: str, labels: tuple[str, ...], *, max_chars: int) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    pattern = re.compile(
        rf"(?:^|\n|[| ])(?:{label_pattern})\s*[：:]\s*(?P<value>[^\n]+)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        value = match.group("value")
        value = value.split("|", 1)[0]
        value = _SNAPSHOT_FIELD_BOUNDARY_RE.split(value, maxsplit=1)[0]
        value = _clean_snapshot_text(value[:max_chars])
        value = _truncate_snapshot_value_at_stop(value)
        if value:
            return value
    return None


def _truncate_snapshot_value_at_stop(value: str) -> str:
    stop_positions = [index for token in _SNAPSHOT_BIO_STOP_TOKENS if (index := value.find(token)) >= 0]
    if not stop_positions:
        return value
    return value[: min(stop_positions)].strip()


def _clean_snapshot_text(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", text)
    text = re.sub(r"\[([^\]]+)\]\((?:mailto:)?[^)]+\)", r"\1", text)
    text = text.replace("\\.", ".")
    text = text.replace("**", "")
    text = text.strip().strip("|").strip()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _snapshot_self_academician_evidence(name: str, title: str | None, bio: str | None, text: str) -> bool:
    if contains_self_academician_hint(name, title, bio):
        return True
    if title and "院士" in title:
        return True
    window = _snapshot_name_context_window(text, name)
    return bool(window and contains_self_academician_hint(name, window))


def _snapshot_name_context_window(text: str, name: str, *, radius: int = 260) -> str:
    index = text.find(name)
    if index < 0:
        return ""
    return text[max(0, index - radius) : min(len(text), index + len(name) + radius)]


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
    existing_detail_task_urls = await _load_existing_detail_task_urls(self, current)
    db_candidates = await _load_homepage_backfill_detail_urls(
        self,
        current,
        fetched.url,
        existing_detail_task_urls=existing_detail_task_urls,
    )
    candidates = _merge_ordered_urls(db_candidates, candidates)
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
    skipped_existing_task = 0
    for link in candidates:
        if len(pending) >= remaining:
            break
        normalized = _sanitize_url(link)
        if not normalized:
            continue
        if normalized in existing_detail_task_urls:
            skipped_existing_task += 1
            continue
        if normalized in reserved:
            skipped_reserved += 1
            continue
        if normalized in self._detail_visited_urls or normalized in self.visited_urls:
            continue
        if enriched_names and _anchor_matches_enriched_name(sig_by_url.get(normalized) or sig_by_url.get(link), enriched_names):
            skipped_by_name += 1
            continue
        self._detail_visited_urls.add(normalized)
        pending.append(normalized)

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
    if skipped_existing_task:
        self._pipeline_stats["detail_links_skipped_existing_task"] = int(
            self._pipeline_stats.get("detail_links_skipped_existing_task", 0)
        ) + skipped_existing_task
        self.logger.debug(
            "Detail enrichment skipped %s links with existing detail tasks org_unit=%s page=%s",
            skipped_existing_task,
            current.label or "Unknown",
            fetched.url,
        )

    if not pending:
        if candidates and skipped_reserved < len(candidates):
            self._pipeline_stats["detail_pending_empty_with_candidates"] = int(
                self._pipeline_stats.get("detail_pending_empty_with_candidates", 0)
            ) + 1
            sample_visited = [
                c
                for c in candidates
                if c in self._detail_visited_urls or c in self.visited_urls or c in existing_detail_task_urls
            ][:3]
            self.logger.warning(
                "Detail enrichment found %s candidates but produced 0 pending org_unit=%s page=%s "
                "(all already visited, already tasked, or matched enriched names; sample=%s skipped_by_name=%s "
                "skipped_existing_task=%s)",
                len(candidates),
                current.label or "Unknown",
                fetched.url,
                sample_visited,
                skipped_by_name,
                skipped_existing_task,
            )
        return
    self._detail_processed_by_org_unit[org_unit_key] = processed + len(pending)
    await self._process_detail_urls_with_human(pending, current, skills)
    # Refresh enriched-name cache so subsequent list pages benefit from
    # whatever detail extraction just succeeded.
    self._enriched_names_by_org_unit.pop(org_unit_key, None)


async def process_detail_urls_with_human(self: Any, urls: list[str], current: Any, skills: str) -> None:
    next_depth = current.depth + 1
    allow_profile_depth = not self._within_depth(next_depth)
    if allow_profile_depth and next_depth > getattr(self, "max_depth", 0) + 1:
        return
    for url in urls:
        if allow_profile_depth and not _is_same_site_primary_profile_url(
            url,
            start_url=getattr(self, "start_url", ""),
        ):
            self._pipeline_stats["detail_profile_depth_gate_skipped"] = int(
                self._pipeline_stats.get("detail_profile_depth_gate_skipped", 0)
            ) + 1
            continue
        if url in self.visited_urls:
            continue
        fetched = await self._fetch_url(url, next_depth, allow_depth_excess=allow_profile_depth)
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
                requested_url=url,
            )
            continue
        await self._extract_professors_from_page(
            current,
            fetched,
            skills,
            detail_mode=True,
            requested_url=url,
        )


def _merge_ordered_urls(primary: list[str], secondary: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for url in [*primary, *secondary]:
        normalized = _sanitize_url(url)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return merged


async def _load_existing_detail_task_urls(self: Any, current: Any) -> set[str]:
    org_unit_name = (getattr(current, "label", "") or "").strip()
    org_unit_id = getattr(current, "org_unit_id", None)
    if not org_unit_name and org_unit_id is None:
        return set()
    async with self.db.session() as session:
        if not org_unit_name and org_unit_id is not None:
            org_unit = await session.get(OrgUnit, int(org_unit_id))
            org_unit_name = (getattr(org_unit, "name", "") or "").strip()
        if not org_unit_name:
            return set()
        existing_filters = [CrawlTask.task_kind == CrawlTaskKind.DETAIL_PAGE.value]
        existing_filters.append(CrawlTask.org_unit_name == org_unit_name)
        rows = (
            await session.execute(select(CrawlTask.source_url).where(*existing_filters))
        ).scalars().all()
    return {_sanitize_url(url) for url in rows if _sanitize_url(url)}


async def _load_homepage_backfill_detail_urls(
    self: Any,
    current: Any,
    current_url: str,
    *,
    existing_detail_task_urls: set[str] | None = None,
) -> list[str]:
    org_unit_name = (getattr(current, "label", "") or "").strip()
    org_unit_id = getattr(current, "org_unit_id", None)
    if not org_unit_name and org_unit_id is None:
        return []
    async with self.db.session() as session:
        filters = [
            Professor.homepage.is_not(None),
            func.trim(Professor.homepage) != "",
            or_(
                Professor.research_areas.is_(None),
                func.trim(Professor.research_areas) == "",
                Professor.bio.is_(None),
                func.trim(Professor.bio) == "",
            ),
        ]
        if org_unit_id is not None:
            filters.append(ProfessorAffiliation.org_unit_id == int(org_unit_id))
            statement = (
                select(Professor.homepage)
                .join(ProfessorAffiliation, ProfessorAffiliation.professor_id == Professor.id)
                .where(*filters)
                .order_by(Professor.id.asc())
            )
        else:
            filters.append(Professor.org_unit_name == org_unit_name)
            statement = select(Professor.homepage).where(*filters).order_by(Professor.id.asc())
        rows = (await session.execute(statement)).scalars().all()

    urls: list[str] = []
    seen: set[str] = set()
    existing_detail_urls = existing_detail_task_urls or set()
    for raw_url in rows:
        homepage = _normalize_homepage_backfill_url(
            raw_url,
            current_url=current_url,
            start_url=getattr(self, "start_url", ""),
        )
        if not homepage or homepage in seen or homepage in existing_detail_urls:
            continue
        seen.add(homepage)
        urls.append(homepage)
    if urls:
        self._pipeline_stats["detail_backfill_homepages_found"] = int(
            self._pipeline_stats.get("detail_backfill_homepages_found", 0)
        ) + len(urls)
        self.logger.debug(
            "Detail homepage backfill found %s urls org_unit=%s page=%s sample=%s",
            len(urls),
            org_unit_name or org_unit_id or "Unknown",
            current_url,
            urls[:3],
        )
    return urls


def _normalize_homepage_backfill_url(raw_url: Any, *, current_url: str, start_url: str) -> str | None:
    homepage = normalize_professor_homepage(raw_url)
    if not homepage:
        return None
    if not _is_same_site_primary_profile_url(homepage, start_url=start_url or current_url):
        return None
    if current_url and not _same_site(homepage, current_url):
        return None
    return homepage


def _is_same_site_primary_profile_url(url: str, *, start_url: str) -> bool:
    homepage = normalize_professor_homepage(url)
    if not homepage:
        return False
    if _is_faculty_platform(homepage):
        return False
    if start_url and not _same_site(homepage, start_url):
        return False
    return _looks_like_profile_detail_url(homepage)


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
        related_by_hint = looks_like_profile_detail or any(token in lowered for token in detail_hints)
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
        if _looks_like_profile_detail_url(url):
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
