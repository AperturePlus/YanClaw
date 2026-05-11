from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urldefrag, urljoin, urlparse


_SPACE_RE = re.compile(r"\s+")
_NAV_HINTS = ("nav", "menu", "tab", "tree", "list", "catalog", "channel", "column")


@dataclass(frozen=True)
class LinkSignal:
    url: str
    anchor_text: str = ""
    heading_text: str = ""
    parent_tags_or_classes: tuple[str, ...] = ()
    link_order: int = 0


def extract_links_with_signals(html: str, base_url: str) -> tuple[list[str], tuple[LinkSignal, ...]]:
    if not html:
        return [], ()

    parser = _StructuralLinkParser(base_url=base_url)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return [], ()

    deduped: dict[str, LinkSignal] = {}
    for raw in parser.signals:
        normalized = _normalize_url(raw.url, base_url)
        if not normalized:
            continue
        existing = deduped.get(normalized)
        if existing is None:
            deduped[normalized] = LinkSignal(
                url=normalized,
                anchor_text=raw.anchor_text,
                heading_text=raw.heading_text,
                parent_tags_or_classes=raw.parent_tags_or_classes,
                link_order=raw.link_order,
            )
            continue

        merged = LinkSignal(
            url=normalized,
            anchor_text=existing.anchor_text or raw.anchor_text,
            heading_text=existing.heading_text or raw.heading_text,
            parent_tags_or_classes=existing.parent_tags_or_classes or raw.parent_tags_or_classes,
            link_order=min(existing.link_order, raw.link_order),
        )
        deduped[normalized] = merged

    ordered = sorted(deduped.values(), key=lambda item: (item.link_order, item.url.lower()))
    return [item.url for item in ordered], tuple(ordered)


def _normalize_space(value: str) -> str:
    return _SPACE_RE.sub(" ", value or "").strip()


def _normalize_url(href: str, base_url: str) -> str:
    absolute = urldefrag(urljoin(base_url, href))[0]
    scheme = urlparse(absolute).scheme.lower()
    if scheme not in {"http", "https"}:
        return ""
    return absolute


def _context_token(tag: str, attrs: dict[str, str]) -> str:
    classes = _normalize_space(attrs.get("class", ""))
    elem_id = _normalize_space(attrs.get("id", ""))
    parts = [tag.lower()]
    if classes:
        first = classes.split(" ")[0]
        parts.append(first.lower())
    elif elem_id:
        parts.append(elem_id.lower())
    return ".".join(parts)


def _heading_tag(tag: str) -> bool:
    lowered = tag.lower()
    return lowered in {"h1", "h2", "h3", "h4", "h5", "h6"}


@dataclass
class _OpenAnchor:
    href: str
    heading_text: str
    parent_context: tuple[str, ...]
    order: int
    chunks: list[str]


@dataclass
class _PendingHeading:
    tag: str
    chunks: list[str]


class _StructuralLinkParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.signals: list[LinkSignal] = []
        self._stack: list[str] = []
        self._current_anchor: _OpenAnchor | None = None
        self._current_heading: _PendingHeading | None = None
        self._latest_heading = ""
        self._order = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {str(key).lower(): str(value or "") for key, value in attrs}
        token = _context_token(tag, attr_map)
        self._stack.append(token)

        lowered_tag = tag.lower()
        if _heading_tag(lowered_tag):
            self._current_heading = _PendingHeading(tag=lowered_tag, chunks=[])

        if lowered_tag != "a":
            return
        href = attr_map.get("href", "").strip()
        if not href:
            return
        self._order += 1
        self._current_anchor = _OpenAnchor(
            href=href,
            heading_text=self._latest_heading,
            parent_context=self._collect_parent_context(),
            order=self._order,
            chunks=[],
        )

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "a" and self._current_anchor is not None:
            anchor_text = _normalize_space("".join(self._current_anchor.chunks))
            self.signals.append(
                LinkSignal(
                    url=self._current_anchor.href,
                    anchor_text=anchor_text,
                    heading_text=self._current_anchor.heading_text,
                    parent_tags_or_classes=self._current_anchor.parent_context,
                    link_order=self._current_anchor.order,
                )
            )
            self._current_anchor = None

        if self._current_heading is not None and lowered == self._current_heading.tag:
            self._latest_heading = _normalize_space("".join(self._current_heading.chunks))
            self._current_heading = None

        if self._stack:
            self._stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self._current_heading is not None:
            self._current_heading.chunks.append(data)
        if self._current_anchor is not None:
            self._current_anchor.chunks.append(data)

    def _collect_parent_context(self) -> tuple[str, ...]:
        context: list[str] = []
        for token in reversed(self._stack[:-1]):
            lowered = token.lower()
            if any(hint in lowered for hint in _NAV_HINTS):
                context.append(lowered)
            if len(context) >= 4:
                break
        if not context and self._stack:
            context.append(self._stack[-1].lower())
        return tuple(context)
