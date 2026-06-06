from __future__ import annotations

import re


STRONG_NOISE_URL_TOKENS = (
    "/news",
    "/notice",
    "/tzgg",
    "/gonggao",
    "/announcement",
    "/policy",
    "/zcwj",
    "规章制度",
    "/renshi",
    "/rszc",
    "/hr",
    "/rczp",
    "/zhaopin",
    "/jobs",
    "/dangjian",
    "/party",
    "/xsgz",
    "/zsjy",
)

FACULTY_SIGNAL_TOKENS = (
    "faculty",
    "teacher",
    "staff",
    "professor",
    "research",
    "email",
    "phone",
    "导师",
    "教师",
    "师资",
    "教授",
    "副教授",
    "讲师",
    "研究员",
    "邮箱",
    "电话",
    "研究方向",
    "博导",
    "硕导",
)

STRONG_FACULTY_EVIDENCE_TOKENS = (
    "email",
    "mail",
    "phone",
    "tel",
    "professor",
    "associate professor",
    "assistant professor",
    "lecturer",
    "researcher",
    "导师",
    "教师",
    "教授",
    "副教授",
    "讲师",
    "研究员",
    "邮箱",
    "电话",
    "博导",
    "硕导",
)

NOISE_TEXT_TOKENS = (
    "通知",
    "公告",
    "新闻",
    "政策",
    "规章制度",
    "规章",
    "招聘",
    "人事",
    "党建",
    "招生",
    "就业",
    "notice",
    "announcement",
    "news",
    "policy",
    "recruit",
    "personnel",
    "hr",
)

NOTICE_ISSUANCE_TITLE_RE = re.compile(
    r"^关于印发.{1,160}?的通知(?:[（(【\[].{0,80}[\)）】\]])?(?:[。.!！])?$"
)

EVENT_KICKOFF_NOISE_PHRASES = (
    "活动正式拉开帷幕",
)

RECENT_NEWS_OPENING_PREFIXES = (
    "近日我院",
    "近日我校",
    "近日，"
)


def should_skip_professor_llm(*, url: str, text: str) -> tuple[bool, str]:
    lowered_url = (url or "").lower()
    lowered_text = (text or "").lower()

    if looks_like_notice_issuance_page(text):
        return True, "notice_issuance_title"
    if looks_like_event_kickoff_noise_page(text):
        return True, "event_kickoff_phrase"
    if looks_like_recent_school_news_opening(text):
        return True, "recent_school_news_opening"

    has_faculty_signal = ("@" in (text or "")) or any(token in lowered_text for token in FACULTY_SIGNAL_TOKENS)
    evidence_hits = sum(1 for token in STRONG_FACULTY_EVIDENCE_TOKENS if token in lowered_text)
    has_strong_faculty_evidence = ("@" in (text or "")) or evidence_hits >= 2
    if any(token in lowered_url for token in STRONG_NOISE_URL_TOKENS) and not has_strong_faculty_evidence:
        return True, "url_noise_token"

    lines = [line.strip() for line in re.split(r"[\r\n]+", text or "") if line.strip()]
    if not lines:
        return False, ""
    noise_hits = sum(1 for line in lines if any(token in line.lower() for token in NOISE_TEXT_TOKENS))
    noise_ratio = noise_hits / float(len(lines))
    if noise_ratio >= 0.35 and not has_faculty_signal:
        return True, f"text_noise_ratio={noise_ratio:.2f}"
    return False, ""


def looks_like_notice_issuance_page(text: str) -> bool:
    lines = [line.strip() for line in re.split(r"[\r\n]+", text or "") if line.strip()]
    for line in lines[:8]:
        normalized = normalize_notice_issuance_title_candidate(line)
        if normalized and NOTICE_ISSUANCE_TITLE_RE.search(normalized):
            return True
    return False


def normalize_notice_issuance_title_candidate(line: str) -> str:
    cleaned = str(line or "").strip()
    cleaned = re.sub(r"^\s*#{1,6}\s*", "", cleaned)
    cleaned = re.sub(r"^\s*(?:当前位置|您现在的位置|位置)\s*[:：].*?[>›»]\s*", "", cleaned)
    cleaned = re.sub(r"^\s*(?:标题|题目)\s*[:：]\s*", "", cleaned)
    cleaned = re.sub(r"\s+", "", cleaned)
    cleaned = cleaned.strip(" \t\r\n\"'“”‘’")
    return cleaned


def looks_like_event_kickoff_noise_page(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    return any(phrase in compact for phrase in EVENT_KICKOFF_NOISE_PHRASES)


def looks_like_recent_school_news_opening(text: str) -> bool:
    lines = [line.strip() for line in re.split(r"[\r\n]+", text or "") if line.strip()]
    for line in lines[:8]:
        normalized = normalize_news_opening_candidate(line)
        if any(normalized.startswith(prefix) for prefix in RECENT_NEWS_OPENING_PREFIXES):
            return True
    return False


def normalize_news_opening_candidate(line: str) -> str:
    cleaned = str(line or "").strip()
    cleaned = re.sub(r"^\s*#{1,6}\s*", "", cleaned)
    cleaned = re.sub(r"^\s*(?:当前位置|您现在的位置|位置)\s*[:：].*?[>›»]\s*", "", cleaned)
    cleaned = re.sub(r"^\s*(?:标题|题目)\s*[:：]\s*", "", cleaned)
    cleaned = re.sub(r"^[\s\"'“”‘’]+", "", cleaned)
    cleaned = re.sub(r"[\s,，、]+", "", cleaned)
    return cleaned
