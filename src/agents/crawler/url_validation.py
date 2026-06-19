from __future__ import annotations

from html import unescape
from typing import Any
from urllib.parse import urldefrag, urljoin, urlparse, unquote


_BLOCKED_URL_TEXT_MARKERS = (
    "\u8f6c\u6362\u94fe\u63a5\u9519\u8bef",  # CMS "link conversion error" marker.
)


def normalize_crawlable_url(value: Any, *, base_url: str | None = None) -> str:
    """Return a defragmented HTTP(S) URL that is safe to hand to a fetcher."""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return ""
    if not isinstance(value, str):
        return ""

    raw = value.strip()
    if not raw or _contains_malformed_url_content(raw):
        return ""

    url = urljoin(base_url, raw) if base_url else raw
    url = urldefrag(url.strip())[0]
    if not url or _contains_malformed_url_content(url):
        return ""

    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        return ""
    if not parsed.hostname:
        return ""
    return url


def is_crawlable_url(value: Any, *, base_url: str | None = None) -> bool:
    return bool(normalize_crawlable_url(value, base_url=base_url))


def _contains_malformed_url_content(value: str) -> bool:
    if any(char in value for char in "\r\n\t\x00"):
        return True
    for candidate in _decoded_variants(value):
        if "<" in candidate or ">" in candidate:
            return True
        if any(marker in candidate for marker in _BLOCKED_URL_TEXT_MARKERS):
            return True
    return False


def _decoded_variants(value: str) -> tuple[str, ...]:
    variants: list[str] = []
    pending = [value]
    pending.append(unescape(value))
    pending.append(unquote(value, errors="replace"))
    pending.append(unescape(unquote(value, errors="replace")))
    pending.append(unquote(unescape(value), errors="replace"))

    seen: set[str] = set()
    for item in pending:
        if item in seen:
            continue
        seen.add(item)
        variants.append(item)
    return tuple(variants)
