from __future__ import annotations

import re
from typing import Any, Mapping


_NULLISH_TEXTS = {
    "",
    "-",
    "--",
    "n/a",
    "na",
    "none",
    "null",
    "nan",
    "暂无",
    "无",
    "未知",
    "未公开",
}

_TITLE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("院士", ("院士", "academician")),
    ("副主任医师", ("副主任医师", "associate chief physician")),
    ("主任医师", ("主任医师", "chief physician")),
    ("主治医师", ("主治医师", "attending physician")),
    ("住院医师", ("住院医师", "resident physician")),
    ("副教授", ("副教授", "associate professor")),
    ("助理教授", ("助理教授", "assistant professor")),
    ("教授", ("教授", "professor", "chair professor")),
    ("副研究员", ("副研究员", "associate researcher")),
    ("助理研究员", ("助理研究员", "assistant researcher")),
    ("研究员", ("研究员", "researcher")),
    ("讲师", ("讲师", "lecturer")),
    ("工程师", ("工程师", "engineer")),
]

_ENROLLMENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("博士生导师", ("博士生导师", "博导", "phd supervisor", "doctoral advisor")),
    ("硕士生导师", ("硕士生导师", "硕导", "master supervisor", "masters supervisor")),
]

_ACADEMICIAN_HINTS = ("院士", "academician")
_TRUTHY_VALUES = {"1", "true", "yes", "y", "是", "对", "院士"}
_ACADEMICIAN_RELATION_HINTS = (
    "导师",
    "师从",
    "合作导师",
    "合作学者",
    "合作对象",
    "领衔",
)
_ACADEMICIAN_RELATION_RE = re.compile(
    r"(?:导师|师从|合作导师|合作学者|合作对象|(?:^|[，,。；;\s])与[^。；;\n\r]{0,80}?院士|"
    r"院士[^。；;\n\r]{0,40}?(?:领衔|团队|课题组|工作站|专家工作站)|"
    r"院士(?:团队|课题组|工作站|专家工作站))",
    re.IGNORECASE,
)
_SELF_ACADEMICIAN_IDENTITY_RE = re.compile(
    r"(?:当选|入选|增选|聘为|受聘为|现为|是|为|担任)[^。；;\n\r]{0,50}?(?:院士|academician)",
    re.IGNORECASE,
)
_DIRECT_ACADEMICIAN_PHRASE_RE = re.compile(
    r"(?:中国科学院|中国工程院|两院|科学院|工程院|外籍|美国艺术与科学院|法国科学院|"
    r"荷兰皇家科学院|加拿大工程院|欧洲科学院|IEEE\s*)?院士",
    re.IGNORECASE,
)
_RETIRED_HINTS = (
    "离退休",
    "退休",
    "荣休",
    "退休教师",
    "离休",
    "退休人员",
    "retired",
    "emeritus",
)
_SEPARATOR_RE = re.compile(r"[;,，；、/|]+")
_BULLET_PREFIX_RE = re.compile(r"^[\s\d\.\-、:：\)\(]+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]")
_CJK_CHAR = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0002ebef"
_CJK_INNER_SPACE_RE = re.compile(rf"(?<=[{_CJK_CHAR}])\s+(?=[{_CJK_CHAR}])")
_TRAILING_LOW_VALUE_NAME_MARKER_RE = re.compile(r"\s*[\(（]\s*(?:兼|兼职)\s*[\)）]\s*$")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z]")
_TRAILING_CJK_ALIAS_RE = re.compile(
    rf"^(?P<base>.+?)\s*[\(（]\s*(?P<alias>[{_CJK_CHAR}][{_CJK_CHAR}\s·・]*)\s*[\)）]\s*$"
)
_RESEARCH_LABEL_RE = re.compile(
    r"(?:研究方向|研究领域|主要研究方向|主要研究领域|研究兴趣|主要研究兴趣)\s*[:：]\s*(?P<value>[^。\n\r]+)"
)
_RESEARCH_AREA_SEPARATOR_RE = re.compile(r"[；;\n\r|]+")
_RESEARCH_CONTACT_LABEL_PREFIX_RE = re.compile(
    r"^(?:e-?mail|mail|email\s+address|邮箱|电子邮箱|电子邮件|phone|tel|telephone|"
    r"电话|办公电话|联系电话|homepage|home\s+page|website|个人主页|主页|网址)\s*[：:]",
    re.IGNORECASE,
)
_EMAIL_VALUE_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
_URL_VALUE_RE = re.compile(r"https?://[^\s\])>\"']+", re.IGNORECASE)
_PHONE_VALUE_RE = re.compile(r"(?:\+?\d[\d\-()（） ]{5,}\d)")
_RESEARCH_CONTACT_LABELS = {
    "email",
    "e-mail",
    "mail",
    "emailaddress",
    "邮箱",
    "电子邮箱",
    "电子邮件",
    "phone",
    "tel",
    "telephone",
    "联系电话",
    "电话",
    "办公电话",
    "homepage",
    "home page",
    "website",
    "个人主页",
    "主页",
    "网址",
}
_RESEARCH_ACHIEVEMENT_RE = re.compile(
    r"在(?P<value>[^。；\n\r]{2,120}?)(?:等)?方面(?:取得|开展|进行|做出)[^。；\n\r]{0,40}?(?:研究成果|成果|研究)"
)
_MAINLY_ENGAGED_RE = re.compile(
    r"主要(?:从事|研究|开展)(?P<value>[^。；\n\r]{2,100}?)(?:等)?(?:方面)?(?:的)?研究"
)
_ADVOCATE_RESEARCH_RE = re.compile(
    r"(?:倡导|推动|率先倡导)(?:进行|开展)?(?P<value>[^。；\n\r]{2,100}?)研究"
)
_NON_PERSON_NAME_HINTS = (
    "办事指南",
    "办事流程",
    "资料下载",
    "申请表",
    "审批表",
    "办理程序",
    "人事政策",
    "薪酬福利",
)


def sanitize_professor_payload(
    record: Mapping[str, Any],
    *,
    org_unit_name: str,
) -> tuple[dict[str, Any], bool]:
    name = normalize_name(record.get("name"))
    if not name:
        raise ValueError("Professor name is required")
    if looks_like_non_person_name(name):
        raise ValueError(f"Invalid professor name: {name}")

    raw_title = _to_text(record.get("title"))
    bio = normalize_optional_text(record.get("bio"))
    enrollment_pref = merge_enrollment_pref(
        normalize_optional_text(record.get("enrollment_pref") or record.get("enrollment_preference")),
        infer_enrollment_pref_from_title(raw_title),
    )
    title = normalize_title(raw_title)

    self_evidence = contains_self_academician_hint(name, raw_title, bio)
    trusted_evidence = is_truthy(record.get("_self_academician_evidence")) or is_truthy(
        record.get("self_academician_evidence")
    )
    is_academician = trusted_evidence or self_evidence or (
        is_truthy(record.get("is_academician")) and self_evidence
    )
    if is_academician:
        title = "院士"
    elif title == "院士":
        title = normalize_non_academician_title(raw_title)
    research_areas = _normalize_research_areas(record.get("research_areas")) or _normalize_research_areas(
        infer_research_areas_from_bio(bio)
    )

    cleaned = {
        "name": name,
        "org_unit_name": normalize_org_unit_name(org_unit_name),
        "title": title,
        "research_areas": research_areas,
        "email": normalize_optional_text(record.get("email")),
        "phone": normalize_optional_text(record.get("phone")),
        "homepage": normalize_optional_text(record.get("homepage")),
        "external_link": normalize_optional_text(record.get("external_link")),
        "bio": bio,
        "enrollment_pref": enrollment_pref,
        "publications": normalize_multivalue(record.get("publications")),
    }
    return cleaned, is_academician


def looks_like_non_person_name(value: Any) -> bool:
    text = normalize_name(value)
    if not text:
        return False
    return any(hint in text for hint in _NON_PERSON_NAME_HINTS)


def is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = _to_text(value).lower()
    return text in _TRUTHY_VALUES


def normalize_org_unit_name(value: Any, *, default: str = "Unknown") -> str:
    text = normalize_optional_text(value)
    return text or default


def normalize_name(value: Any) -> str:
    text = _to_text(value)
    if not text:
        return ""
    text = _ZERO_WIDTH_RE.sub("", text).replace("\u3000", " ")
    text = _BULLET_PREFIX_RE.sub("", text)
    text = " ".join(text.split())
    text = _CJK_INNER_SPACE_RE.sub("", text)
    while True:
        cleaned = _TRAILING_LOW_VALUE_NAME_MARKER_RE.sub("", text).strip()
        if cleaned == text:
            break
        text = cleaned
    text = _strip_trailing_latin_cjk_alias(text)
    return text.strip()


def normalize_name_key(value: Any) -> str:
    return normalize_name(value)


def _strip_trailing_latin_cjk_alias(text: str) -> str:
    match = _TRAILING_CJK_ALIAS_RE.match(text)
    if not match:
        return text
    base = match.group("base").strip()
    if not _LATIN_CHAR_RE.search(base):
        return text
    return base


def normalize_title(value: Any) -> str | None:
    text = normalize_optional_text(value)
    if not text:
        return None
    lowered = text.lower()
    compact = lowered.replace(" ", "")
    for canonical, hints in _TITLE_RULES:
        if any(hint in text or hint in lowered or hint in compact for hint in hints):
            return canonical
    return None


def normalize_non_academician_title(value: Any) -> str | None:
    text = normalize_optional_text(value)
    if not text:
        return None
    lowered = text.lower()
    compact = lowered.replace(" ", "")
    for canonical, hints in _TITLE_RULES:
        if canonical == "院士":
            continue
        if any(hint in text or hint in lowered or hint in compact for hint in hints):
            return canonical
    return None


def infer_enrollment_pref_from_title(value: Any) -> str | None:
    text = normalize_optional_text(value)
    if not text:
        return None
    lowered = text.lower()
    compact = lowered.replace(" ", "")
    hits: list[str] = []
    for canonical, hints in _ENROLLMENT_RULES:
        if any(hint in text or hint in lowered or hint in compact for hint in hints):
            hits.append(canonical)
    if not hits:
        return None
    # Preserve rule order.
    unique = []
    for item in hits:
        if item not in unique:
            unique.append(item)
    return "；".join(unique)


def merge_enrollment_pref(primary: Any, inferred: Any) -> str | None:
    parts: list[str] = []
    for value in (primary, inferred):
        text = normalize_optional_text(value)
        if not text:
            continue
        for token in _SEPARATOR_RE.split(text):
            token = token.strip()
            if not token:
                continue
            if token not in parts:
                parts.append(token)
    if not parts:
        return None
    return "；".join(parts)


def normalize_multivalue(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        items = [normalize_optional_text(item) for item in value]
        normalized = [item for item in items if item]
        if not normalized:
            return None
        unique: list[str] = []
        for item in normalized:
            if item not in unique:
                unique.append(item)
        return "；".join(unique)
    text = normalize_optional_text(value)
    if not text:
        return None
    return text


def _normalize_research_areas(value: Any) -> str | None:
    text = normalize_multivalue(value)
    if not text:
        return None
    terms: list[str] = []
    for part in _RESEARCH_AREA_SEPARATOR_RE.split(text):
        term = normalize_optional_text(part)
        if not term:
            continue
        term = term.strip("：:，,；;。. ")
        if not term or _looks_like_research_contact_value(term):
            continue
        if term not in terms:
            terms.append(term)
    return "；".join(terms) if terms else None


def _looks_like_research_contact_value(value: str) -> bool:
    text = _to_text(value).strip("：:，,；;。. ")
    if not text:
        return True
    lowered = text.lower()
    compact = re.sub(r"[\s_\-]+", "", lowered)
    if lowered in _RESEARCH_CONTACT_LABELS or compact in _RESEARCH_CONTACT_LABELS:
        return True
    if _EMAIL_VALUE_RE.search(text) or _URL_VALUE_RE.search(text):
        return True
    if _RESEARCH_CONTACT_LABEL_PREFIX_RE.match(text):
        return True
    return bool(_PHONE_VALUE_RE.fullmatch(text))


def infer_research_areas_from_bio(value: Any) -> str | None:
    text = normalize_optional_text(value)
    if not text:
        return None

    terms: list[str] = []
    for match in _RESEARCH_LABEL_RE.finditer(text):
        _extend_research_terms(terms, match.group("value"), split_conjunction=True)
    for match in _RESEARCH_ACHIEVEMENT_RE.finditer(text):
        _extend_research_terms(terms, match.group("value"), split_conjunction=True)
    for match in _MAINLY_ENGAGED_RE.finditer(text):
        _extend_research_terms(terms, match.group("value"), split_conjunction=True)
    for match in _ADVOCATE_RESEARCH_RE.finditer(text):
        _extend_research_terms(terms, match.group("value"), split_conjunction=False)

    if not terms:
        return None
    return "；".join(terms[:8])


def _extend_research_terms(target: list[str], value: Any, *, split_conjunction: bool) -> None:
    text = normalize_optional_text(value)
    if not text:
        return
    text = _clean_research_phrase(text)
    if not text:
        return
    split_re = r"[；;、，,]" if not split_conjunction else r"[；;、，,]|和"
    for part in re.split(split_re, text):
        term = _clean_research_phrase(part)
        if not term:
            continue
        if len(term) > 40:
            continue
        if term not in target:
            target.append(term)


def _clean_research_phrase(value: Any) -> str:
    text = _to_text(value)
    if not text:
        return ""
    text = re.sub(r"\s+", "", text)
    text = text.strip("：:，,；;。.")
    text = re.sub(r"^(?:其|相关|有关|围绕)", "", text)
    text = re.sub(r"(?:等方面|方面|等领域|领域|等|的研究|研究)$", "", text)
    text = text.strip("：:，,；;。.")
    if len(text) < 2:
        return ""
    low_value_markers = (
        "获奖",
        "成果奖",
        "教学成果",
        "博士生",
        "硕士生",
        "委员会",
        "理事长",
        "政协委员",
        "顾问",
    )
    if any(marker in text for marker in low_value_markers):
        return ""
    return text


def normalize_optional_text(value: Any) -> str | None:
    text = _to_text(value)
    if not text:
        return None
    lowered = text.lower()
    if lowered in _NULLISH_TEXTS:
        return None
    return text


def contains_academician_hint(*values: Any) -> bool:
    for value in values:
        text = normalize_optional_text(value)
        if not text:
            continue
        lowered = text.lower()
        if any(hint in text or hint in lowered for hint in _ACADEMICIAN_HINTS):
            return True
    return False


def contains_self_academician_hint(name: Any, *values: Any) -> bool:
    normalized_name = normalize_name(name)
    for value in values:
        text = normalize_optional_text(value)
        if not text or not contains_academician_hint(text):
            continue
        if _looks_like_academician_title(text):
            return True
        if normalized_name and _contains_named_self_academician_hint(normalized_name, text):
            return True
        if _contains_pronominal_self_academician_hint(text):
            return True
        if normalized_name and _contains_english_self_academician_hint(normalized_name, text):
            return True
    return False


def _looks_like_academician_title(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if len(compact) > 80:
        return False
    if _ACADEMICIAN_RELATION_RE.search(compact):
        return False
    if any(marker in compact for marker in ("导师", "师从", "合作", "团队", "工作站", "课题组", "领衔")):
        return False
    return bool(_DIRECT_ACADEMICIAN_PHRASE_RE.search(compact) or "academician" in compact.lower())


def _contains_named_self_academician_hint(name: str, text: str) -> bool:
    compact_text = re.sub(r"\s+", "", text)
    compact_name = re.sub(r"\s+", "", name)
    if compact_name and f"{compact_name}院士" in compact_text:
        index = compact_text.find(f"{compact_name}院士")
        if not _academician_mention_is_relational(compact_text[max(0, index - 30) : index + len(compact_name) + 20]):
            return True

    for match in re.finditer(re.escape(compact_name) + r"[^。；;\n\r]{0,100}?院士", compact_text):
        segment = match.group(0)
        if not _academician_mention_is_relational(segment):
            return True
    return False


def _contains_pronominal_self_academician_hint(text: str) -> bool:
    for sentence in re.split(r"[。；;\n\r]+", text):
        sentence = sentence.strip()
        if not sentence or not contains_academician_hint(sentence):
            continue
        if _academician_mention_is_relational(sentence):
            continue
        if _SELF_ACADEMICIAN_IDENTITY_RE.search(sentence):
            return True
        if len(sentence) <= 70 and _DIRECT_ACADEMICIAN_PHRASE_RE.search(sentence):
            return True
    return False


def _contains_english_self_academician_hint(name: str, text: str) -> bool:
    lowered = re.sub(r"\s+", " ", text.lower())
    lowered_name = re.sub(r"\s+", " ", name.lower()).strip()
    if not lowered_name or "academician" not in lowered:
        return False
    if re.search(r"(advisor|adviser|mentor|supervisor|collaborat).{0,80}?academician", lowered):
        return False
    if f"academician {lowered_name}" in lowered:
        return True
    if re.search(re.escape(lowered_name) + r".{0,80}?academician", lowered):
        return True
    return bool(re.search(r"(?:elected|appointed|selected).{0,60}?academician", lowered))


def _academician_mention_is_relational(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    relation_text = re.sub(r"(?:博士生导师|硕士生导师|研究生导师|博士导师|硕士导师|博导|硕导)", "", compact)
    lowered = relation_text.lower()
    if _ACADEMICIAN_RELATION_RE.search(relation_text):
        return True
    if any(hint in relation_text for hint in _ACADEMICIAN_RELATION_HINTS):
        return True
    return bool(re.search(r"(advisor|adviser|mentor|supervisor|collaborat).{0,80}?academician", lowered))


def contains_retired_hint(*values: Any) -> bool:
    for value in values:
        text = normalize_optional_text(value)
        if not text:
            continue
        lowered = text.lower()
        if any(hint in text or hint in lowered for hint in _RETIRED_HINTS):
            return True
    return False


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    text = text.strip("`").strip("*").strip("_").strip('"').strip("'")
    return text.strip()
