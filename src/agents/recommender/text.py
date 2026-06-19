from __future__ import annotations

import re
from collections import Counter
from hashlib import sha1
from typing import Iterable


_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_LATIN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_+\-.]{1,}")
_SPLIT_RE = re.compile(r"[\s,，;；、/|｜\n\r\t:：()（）\[\]【】<>《》]+")
_STOP_TERMS = {
    "老师",
    "导师",
    "教授",
    "学院",
    "学校",
    "大学",
    "专业",
    "方向",
    "研究",
    "学生",
    "希望",
    "申请",
    "本科",
    "硕士",
    "博士",
    "个人",
    "简历",
    "项目",
    "论文",
    "经历",
    "招生",
    "工作",
    "教育",
    "能力",
    "the",
    "and",
    "for",
    "with",
    "from",
}


def normalize_term(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip("`*_\"'.,;:!?，。；：！？、")


def concept_key(term: str) -> str:
    normalized = normalize_term(term)
    digest = sha1(normalized.encode("utf-8")).hexdigest()[:16]
    return f"concept:{digest}"


def tokenize(text: str, *, max_cjk_ngram: int = 6) -> list[str]:
    raw = str(text or "")
    tokens: list[str] = []
    for match in _LATIN_RE.finditer(raw):
        token = normalize_term(match.group(0))
        if _is_useful_token(token):
            tokens.append(token)

    for match in _CJK_RE.finditer(raw):
        segment = match.group(0)
        if 2 <= len(segment) <= 12 and _is_useful_token(segment):
            tokens.append(segment)
        upper = min(max_cjk_ngram, len(segment))
        for size in range(2, upper + 1):
            for index in range(0, len(segment) - size + 1):
                token = segment[index : index + size]
                if _is_useful_token(token):
                    tokens.append(token)
    return tokens


def top_terms(text: str, *, limit: int = 24) -> list[str]:
    counts = Counter(tokenize(text))
    ordered = sorted(counts.items(), key=lambda item: (-item[1], -len(item[0]), item[0]))
    return [term for term, _count in ordered[:limit]]


def extract_concepts(*values: str | None, limit: int = 16) -> list[str]:
    concepts: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        for part in _SPLIT_RE.split(text):
            term = normalize_term(part)
            if _is_concept(term) and term not in concepts:
                concepts.append(term)
            if len(concepts) >= limit:
                return concepts
        for term in top_terms(text, limit=limit):
            if _is_concept(term) and term not in concepts:
                concepts.append(term)
            if len(concepts) >= limit:
                return concepts
    return concepts


def token_counts(tokens: Iterable[str]) -> dict[str, int]:
    counts = Counter(token for token in tokens if _is_useful_token(token))
    return dict(counts)


def _is_concept(term: str) -> bool:
    if not _is_useful_token(term):
        return False
    if len(term) > 32:
        return False
    return True


def _is_useful_token(token: str) -> bool:
    if not token or token in _STOP_TERMS:
        return False
    if len(token) < 2:
        return False
    if token.isdigit():
        return False
    return True
