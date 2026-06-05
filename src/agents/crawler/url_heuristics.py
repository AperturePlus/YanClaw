from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import parse_qsl, unquote, urlparse

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
    "szjs",
    "zzjs",
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

_ORG_UNIT_EXCLUDE_KEYWORD_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "arts",
        (
            "艺术",
            "美术",
            "音乐",
            "舞蹈",
            "戏剧",
            "戏曲",
            "电影学院",
            "影视学院",
            "传媒艺术",
            "艺术设计",
            "fine arts",
            "visual arts",
            "performing arts",
            "school of arts",
            "college of arts",
            "music",
            "dance",
            "drama",
            "theater",
            "theatre",
            "film",
            "cinema",
        ),
    ),
    (
        "sports",
        (
            "体育",
            "运动训练",
            "竞技体育",
            "physical education",
            "sports",
            "sport science",
            "kinesiology",
            "athletics",
        ),
    ),
    (
        "joint_program",
        (
            "中外合作",
            "中外合办",
            "合作办学",
            "国际联合",
            "联合学院",
            "联合培养",
            "中法工程师",
            "中德工程",
            "中英国际",
            "中美联合",
            "匹兹堡",
            "格拉斯哥",
            "巴黎卓越",
            "密西根",
            "爱丁堡",
            "莱斯特",
            "pittsburgh",
            "glasgow",
            "paris elite",
            "michigan",
            "edinburgh",
            "leicester",
            "joint institute",
            "joint college",
            "international joint",
            "cooperative education",
            "sino-foreign",
            "sino foreign",
        ),
    ),
    (
        "basic_teaching",
        (
            "基教中心",
            "基础教学中心",
            "基础教学部",
            "基础课教学部",
            "公共基础教学部",
            "基础教育中心",
            "公共课教学",
        ),
    ),
    (
        "teaching_experiment_center",
        (
            "教学实验中心",
            "实验教学中心",
            "实践教学中心",
            "实验中心",
            "教学中心",
            "实训中心",
            "工程训练中心",
            "实验实训中心",
            "teaching experiment center",
            "experimental teaching center",
            "practice teaching center",
            "training center",
        ),
    ),
    (
        "continuing_education",
        (
            "继续教育学院",
            "继续教育",
            "成人教育学院",
            "成人教育",
            "成人高等教育",
            "网络教育学院",
            "网络教育",
            "开放教育学院",
            "开放教育",
            "职业与继续教育学院",
            "继续教育与培训",
            "继续教育培训",
            "continuing education",
            "adult education",
            "online education",
        ),
    ),
    (
        "undergraduate_teaching_unit",
        (
            "书院",
            "本科生院",
            "本科生学院",
            "本科教育学院",
            "本科教育",
            "荣誉学院",
            "荣誉书院",
            "通识教育学院",
            "通识教育",
            "新生学院",
            "北航学院",
            "residential college",
            "undergraduate college",
            "honors college",
            "honours college",
            "general education",
        ),
    ),
    (
        "excellent_engineer_program",
        (
            "卓工",
            "卓越工程师",
            "卓越工程师学院",
            "卓越工程师培养",
            "excellent engineer",
            "elite engineer",
        ),
    ),
)

DEFAULT_ORG_UNIT_EXCLUDE_KEYWORDS: tuple[str, ...] = tuple(
    dict.fromkeys(keyword for _, keywords in _ORG_UNIT_EXCLUDE_KEYWORD_GROUPS for keyword in keywords)
)


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


_FACULTY_NOISE_URL_HINTS = (
    "/news/",
    "/xwzx/",
    "/notice/",
    "/tzgg/",
    "/gonggao/",
    "/announcement/",
    "/events/",
    "/event/",
    "/rczp/",
    "/zhaopin/",
    "/jobs/",
    "/job/",
    "/hr/",
    "/renshi/",
    "/rsrc/",
    "/rsc/",
    "/personnel/",
    "/policy/",
    "/zcwj/",
    "/rule/",
    "/rules/",
    "/regulation/",
    "/dangjian/",
    "/party/",
    "/student/",
    "/xsgz/",
    "/zsjy/",
    "/download/",
)

_FACULTY_NOISE_TOKEN_HINTS = frozenset(
    {
        "news",
        "xwzx",
        "notice",
        "tzgg",
        "gonggao",
        "announcement",
        "events",
        "event",
        "rczp",
        "zhaopin",
        "jobs",
        "job",
        "hr",
        "renshi",
        "rsrc",
        "rsc",
        "rszc",
        "personnel",
        "policy",
        "zcwj",
        "rule",
        "rules",
        "regulation",
        "dangjian",
        "party",
        "student",
        "xsgz",
        "zsjy",
        "download",
    }
)

_FACULTY_NOISE_STEM_HINTS = frozenset({"rszc", "tzgg", "xwzx", "rczp", "zcwj", "renshi", "policy"})

_EXPLICIT_FACULTY_DIR_HINTS = (
    "/faculty/",
    "/facultylist",
    "/teacher/",
    "/teachers/",
    "/teacherlist",
    "/staff/",
    "/people/",
    "/szdw/",
    "/szdw.htm",
    "/jsdw/",
    "/szll",
    "/qzjs",
    "/mentor",
    "/supervisor",
    "/jzg",
)


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


def _is_non_faculty_noise_url(url: str) -> bool:
    lowered = url.lower()
    if any(token in lowered for token in _FACULTY_NOISE_URL_HINTS):
        return True
    for token in _iter_url_noise_tokens(lowered):
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


_ORG_UNIT_LISTING_STRONG_HINTS = (
    "/yx.htm",
    "/yxsz",
    "/xysz",
    "/jgsz",
    "/zzjg",
    "/xybm",
    "/jxkydw",
    "zuzhijigou",
    "jiaoxuejigou",
    "jiaoxuedanwei",
    "yuanxishezhi",
    "xueyuanshezhi",
    "college",
    "school",
    "department",
    "academy",
    "xueyuan",
    "yuanxi",
)

_ORG_UNIT_LISTING_NOISE_HINTS = (
    "/xxgk/xxjj",
    "/xxgk/ls",
    "/xxgk/ld",
    "/xxgk/xrld",
    "/xygk/",
    "/about",
    "/overview",
    "/intro",
    "/history",
    "/leader",
    "/news",
    "/notice",
    "/jgbc",
    "/ywdw",
    "/jjjcjg",
)


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


FACULTY_PAGE_TYPE_FULL = "full_list"
FACULTY_PAGE_TYPE_CATEGORY = "category_list"
FACULTY_PAGE_TYPE_ELITE = "elite_list"
FACULTY_PAGE_TYPE_NOISE = "noise_or_login"
FACULTY_PAGE_TYPE_UNKNOWN = "unknown"

_FACULTY_LOGIN_HARD_REJECT_HINTS = (
    "tplloginaccount",
    "/login",
    "login.jsp",
    "/account",
    "signin",
    "xw_list_new",
)

_FACULTY_FULL_TEXT_HINTS = (
    "全体教师",
    "教师名录",
    "师资队伍",
    "教师队伍",
    "专任教师",
    "在职教师",
    "在岗教师",
    "现任教师",
    "faculty list",
    "teacher list",
    "all teachers",
    "teaching staff",
    "staff directory",
)

_FACULTY_FULL_STRONG_TEXT_HINTS = (
    "全体教师",
    "师资队伍",
    "教师队伍",
    "专任教师",
    "在职教师",
    "在岗教师",
    "现任教师",
    "faculty list",
    "all teachers",
    "teaching staff",
    "staff directory",
)

_FACULTY_CATEGORY_TEXT_HINTS = (
    "教授",
    "副教授",
    "讲师",
    "研究员",
    "博导",
    "硕导",
    "博士生导师",
    "硕士生导师",
    "professor",
    "associate professor",
    "assistant professor",
    "lecturer",
    "researcher",
)

_FACULTY_ELITE_TEXT_HINTS = (
    "杰出人才",
    "高层次人才",
    "名师",
    "杰青",
    "优青",
    "academician",
    "distinguished",
    "talent",
    "fellow",
)

_FACULTY_NOISE_TEXT_HINTS = (
    "新闻",
    "通知",
    "公告",
    "党建",
    "人事",
    "招聘",
    "news",
    "notice",
    "announcement",
    "events",
    "policy",
    "recruit",
)

_FACULTY_NAV_CONTEXT_HINTS = ("nav", "menu", "tab", "tree", "list")


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
    noise_hit = _contains_any(signal_text, _FACULTY_NOISE_TEXT_HINTS) or _is_non_faculty_noise_url(url)
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

    page_type = FACULTY_PAGE_TYPE_UNKNOWN
    if noise_hit and score <= 0:
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


_RETIRED_URL_HINTS = (
    "ltx",
    "ltxjs",
    "ltxgz",
    "ltgz",
    "retired",
    "emeritus",
    "rongxiu",
    "tuixiu",
    "rxjzg",
    "rxjs",
    "laojiaoshi",
)

_RETIRED_TEXT_HINTS = (
    "离退休",
    "退休",
    "荣休",
    "退休教师",
    "离休",
    "退休人员",
    "retired",
    "emeritus",
)


_ACTIVE_TEXT_HINTS = (
    "\u5728\u804c",
    "\u4e13\u4efb\u6559\u5e08",
    "\u73b0\u804c",
    "\u5e08\u8d44\u961f\u4f0d",
    "\u5e08\u8d44\u529b\u91cf",
    "\u6559\u5e08\u540d\u5f55",
    "\u5bfc\u5e08\u961f\u4f0d",
    "\u7855\u5bfc",
    "\u535a\u5bfc",
    "teacher",
    "faculty",
    "professor",
    "staff",
    "mentor",
    "supervisor",
)


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


_FOCUS_COMPUTER_HINTS = (
    "computer",
    "computing",
    "computer science",
    "\u8ba1\u7b97\u673a",  # 计算机
    "\u8ba1\u7b97\u673a\u79d1\u5b66",  # 计算机科学
)
_FOCUS_COMPUTER_HOST_LABELS = {"cs", "cse", "computer", "computing"}
_FOCUS_COMPUTER_ASCII_TERMS = {"cs", "cse"}

_FOCUS_SOFTWARE_HINTS = (
    "software",
    "software engineering",
    "\u8f6f\u4ef6",  # 软件
    "\u8f6f\u4ef6\u5de5\u7a0b",  # 软件工程
)
_FOCUS_SOFTWARE_HOST_LABELS = {"software", "se", "sse"}
_FOCUS_SOFTWARE_ASCII_TERMS = {"software"}

_FOCUS_AI_HINTS = (
    "artificial intelligence",
    "machine intelligence",
    "\u4eba\u5de5\u667a\u80fd",  # 人工智能
    "\u667a\u80fd\u79d1\u5b66",  # 智能科学
)
_FOCUS_AI_HOST_LABELS = {"ai", "iai", "aai"}
_FOCUS_AI_ASCII_TERMS = {"ai"}

_FOCUS_ELECTRONICS_HINTS = (
    "electronics",
    "electronic",
    "electrical",
    "microelectronics",
    "information engineering",
    "information science",
    "\u7535\u5b50\u4fe1\u606f",  # 电子信息
    "\u7535\u6c14\u5de5\u7a0b",  # 电气工程
    "\u5fae\u7535\u5b50",  # 微电子
)
_FOCUS_ELECTRONICS_HOST_LABELS = {
    "ee",
    "ece",
    "eie",
    "electronic",
    "electronics",
    "microelectronics",
}
_FOCUS_ELECTRONICS_ASCII_TERMS = {"ee", "ece", "eie"}


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




