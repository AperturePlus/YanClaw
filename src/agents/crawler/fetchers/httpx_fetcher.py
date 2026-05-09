from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterable, Mapping
from urllib.parse import urldefrag, urljoin, urlparse

import html2text


@dataclass(frozen=True)
class FetchResult:
    url: str
    text: str
    links: list[str]
    status_code: int
    block_reason: str | None = None


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)


class Fetcher:
    """Compatibility utility holder (no network fetching behavior)."""

    @staticmethod
    def filter_same_domain(links: Iterable[Any], base_url: str) -> list[str]:
        base_host = (urlparse(base_url).hostname or "").lower()
        base_root = _site_root(base_host)
        filtered: list[str] = []
        seen: set[str] = set()

        for raw_link in links:
            link = _coerce_link(raw_link)
            if not link:
                continue
            parsed = urlparse(link)
            host = (parsed.hostname or "").lower()
            if not host:
                continue
            root = _site_root(host)
            if root != base_root:
                continue
            normalized = urldefrag(link)[0]
            if normalized in seen:
                continue
            seen.add(normalized)
            filtered.append(normalized)
        return filtered

    @staticmethod
    def html_to_text(html: str) -> str:
        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.body_width = 0
        return converter.handle(html).strip()

    @staticmethod
    def extract_links(html: str, base_url: str) -> list[str]:
        parser = _LinkParser()
        parser.feed(html)
        links: list[str] = []
        seen: set[str] = set()
        for href in parser.links:
            absolute = urldefrag(urljoin(base_url, href))[0]
            scheme = urlparse(absolute).scheme.lower()
            if scheme not in {"http", "https"}:
                continue
            if absolute in seen:
                continue
            seen.add(absolute)
            links.append(absolute)
        return links

    # Backward-compatible wrappers for older call sites.
    def _html_to_text(self, html: str) -> str:
        return self.html_to_text(html)

    def _extract_links(self, html: str, base_url: str) -> list[str]:
        return self.extract_links(html, base_url)


def _site_root(host: str) -> str:
    parts = [part for part in host.split(".") if part]
    if len(parts) >= 3 and parts[-1] == "cn" and parts[-2] in {"edu", "ac", "com", "net", "org", "gov"}:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _is_html_content(content_type: str) -> bool:
    """Return True if the Content-Type header indicates HTML content."""
    ct = content_type.lower().split(";")[0].strip()
    return ct in {"text/html", "application/xhtml+xml", ""}


def _coerce_link(value: Any) -> str | None:
    """Coerce LLM-produced link payloads into plain URL strings."""
    if isinstance(value, dict):
        for key in ("url", "href", "link"):
            candidate = value.get(key)
            if isinstance(candidate, (str, bytes)):
                value = candidate
                break
        else:
            return None

    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return None

    if not isinstance(value, str):
        return None

    cleaned = value.strip()
    return cleaned or None


def _is_ssl_error(exc: BaseException) -> bool:
    """Return True if *exc* (or its chain) originates from an SSL failure."""
    import ssl

    cur: BaseException | None = exc
    while cur is not None:
        if isinstance(cur, ssl.SSLError):
            return True
        msg = str(cur).lower()
        if "ssl" in msg or "certificate" in msg:
            return True
        cur = cur.__cause__
    return False


_WAF_BODY_MARKERS = (
    "x-amzn-waf-action",
    "challenge",
    "captcha",
    "security check",
    "web application firewall",
    "waf",
    "bot detection",
    "document.cookie",
    "$_ts",
    "__jsl_clearance",
    "acw_sc__v2",
    "settimeout(",
    "正在验证",
    "安全验证",
    "人机验证",
)

_WAF_STATUS_CODES = {202, 403, 405, 412, 429, 503}


def _detect_block_reason(
    *, status_code: int, body_text: str, headers: Mapping[str, Any] | None = None
) -> str | None:
    """Best-effort WAF/challenge page detection for observability and fallback logic."""

    header_action = ""
    if headers is not None:
        raw = headers.get("x-amzn-waf-action")
        if raw is not None:
            header_action = str(raw).strip().lower()

    lowered = body_text.lower()
    marker_hits = [marker for marker in _WAF_BODY_MARKERS if marker in lowered]

    if header_action in {"challenge", "captcha"}:
        return f"waf_header:{header_action}"

    if status_code in _WAF_STATUS_CODES and marker_hits:
        return f"waf_challenge status={status_code} markers={','.join(marker_hits[:3])}"

    # Some WAFs return 2xx + JS challenge (common on Chinese university portals).
    if status_code == 200 and len(marker_hits) >= 3 and len(body_text) > 2000:
        return f"waf_like_html status=200 markers={','.join(marker_hits[:3])}"

    return None
