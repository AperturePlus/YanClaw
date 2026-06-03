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


def sanitize_professor_payload(
    record: Mapping[str, Any],
    *,
    org_unit_name: str,
) -> tuple[dict[str, Any], bool]:
    name = normalize_name(record.get("name"))
    if not name:
        raise ValueError("Professor name is required")

    raw_title = _to_text(record.get("title"))
    bio = normalize_optional_text(record.get("bio"))
    enrollment_pref = merge_enrollment_pref(
        normalize_optional_text(record.get("enrollment_pref") or record.get("enrollment_preference")),
        infer_enrollment_pref_from_title(raw_title),
    )
    title = normalize_title(raw_title)

    is_academician = contains_academician_hint(raw_title, bio)
    if is_academician:
        title = "院士"

    cleaned = {
        "name": name,
        "org_unit_name": normalize_org_unit_name(org_unit_name),
        "title": title,
        "research_areas": normalize_multivalue(record.get("research_areas")),
        "email": normalize_optional_text(record.get("email")),
        "phone": normalize_optional_text(record.get("phone")),
        "homepage": normalize_optional_text(record.get("homepage")),
        "external_link": normalize_optional_text(record.get("external_link")),
        "bio": bio,
        "enrollment_pref": enrollment_pref,
        "publications": normalize_multivalue(record.get("publications")),
    }
    return cleaned, is_academician


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
    return text.strip()


def normalize_name_key(value: Any) -> str:
    return normalize_name(value)


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
