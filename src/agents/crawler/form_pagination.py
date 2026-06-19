from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from agents.crawler.url_validation import normalize_crawlable_url


_FORM_PAGE_ASSIGN_RE = re.compile(
    r"document\.forms\[['\"](?P<form>[^'\"]+)['\"]\]\.(?P<field>[A-Za-z0-9_]+)\.value\s*=\s*['\"]?(?P<page>\d+)['\"]?",
    re.IGNORECASE,
)
_FORM_SUBMIT_RE = re.compile(r"document\.forms\[['\"](?P<form>[^'\"]+)['\"]\]\.submit\s*\(", re.IGNORECASE)
_GOTO_FIELD_RE = re.compile(r"\b(?P<prefix>[A-Za-z0-9_]*?)GOPAGE\b", re.IGNORECASE)


@dataclass(frozen=True)
class FormPaginationState:
    """Stable action for a form-driven pagination page."""

    state_id: str
    label: str
    page_index: int
    form_name: str
    fields: dict[str, str]
    synthetic_url: str
    url: str
    total_pages: int | None = None
    kind: str = "form_submit"
    submit: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "state_id": self.state_id,
            "label": self.label,
            "page_index": self.page_index,
            "total_pages": self.total_pages,
            "form_name": self.form_name,
            "fields": dict(self.fields),
            "submit": self.submit,
            "synthetic_url": self.synthetic_url,
            "url": self.url,
        }


def extract_form_pagination_states(html: str, current_url: str) -> tuple[FormPaginationState, ...]:
    """Extract absolute page-number form submissions from JavaScript pagination anchors."""

    base_url = normalize_crawlable_url(current_url) or current_url
    if not html or not base_url:
        return ()

    parser = _FormPaginationParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return ()

    current_page = _current_page_from_parser(parser) or _current_page_from_url(base_url) or 1
    assignments: dict[tuple[str, str], set[int]] = {}
    for href in parser.hrefs:
        form, field, page = _parse_absolute_page_assignment(href)
        if not form or not field or page <= 0:
            continue
        assignments.setdefault((form, field), set()).add(page)

    if not assignments:
        return ()

    states: list[FormPaginationState] = []
    seen: set[str] = set()
    for (form_name, page_field), pages in sorted(assignments.items()):
        goto_field = _matching_goto_field(parser.input_names, page_field)
        if goto_field:
            max_page = max(pages)
            page_indexes = range(1, max_page + 1)
        else:
            page_indexes = sorted(pages)

        total_pages = max(page_indexes, default=max(pages))
        for page in page_indexes:
            if page <= 1 or page == current_page:
                continue
            synthetic_url = build_form_pagination_synthetic_url(
                base_url,
                form_name=form_name,
                page_field=page_field,
                page_index=page,
            )
            if synthetic_url in seen:
                continue
            seen.add(synthetic_url)
            states.append(
                FormPaginationState(
                    state_id=f"form:{form_name}:{page_field}:{page}",
                    label=f"{form_name} 第 {page} 页",
                    page_index=page,
                    total_pages=total_pages,
                    form_name=form_name,
                    fields={page_field: str(page)},
                    synthetic_url=synthetic_url,
                    url=base_url,
                )
            )

    return tuple(sorted(states, key=lambda item: (item.form_name.lower(), item.page_index, item.synthetic_url)))


def build_form_pagination_synthetic_url(
    url: str,
    *,
    form_name: str,
    page_field: str,
    page_index: int,
) -> str:
    parsed = urlparse(url)
    params = parse_qsl(parsed.query, keep_blank_values=True)
    params = [(key, value) for key, value in params if not key.startswith("__ycl_")]
    params.extend(
        [
            ("__ycl_kind", "form"),
            ("__ycl_form", form_name),
            ("__ycl_field", page_field),
            ("__ycl_page", str(page_index)),
        ]
    )
    return urlunparse(parsed._replace(query=urlencode(params, doseq=True), fragment=""))


def pagination_state_from_any(value: Any) -> FormPaginationState | None:
    if isinstance(value, FormPaginationState):
        return value
    if not isinstance(value, dict):
        return None
    if str(value.get("kind") or "form_submit") != "form_submit":
        return None
    form_name = str(value.get("form_name") or "").strip()
    synthetic_url = str(value.get("synthetic_url") or "").strip()
    url = str(value.get("url") or "").strip()
    fields_value = value.get("fields") or {}
    if not form_name or not synthetic_url or not url or not isinstance(fields_value, dict):
        return None
    fields = {str(key): str(field_value) for key, field_value in fields_value.items() if str(key)}
    page_index = _int_or_zero(value.get("page_index"))
    if page_index <= 0:
        page_index = _int_or_zero(next(iter(fields.values()), "0"))
    if page_index <= 0 or not fields:
        return None
    total_pages = _int_or_zero(value.get("total_pages")) or None
    return FormPaginationState(
        state_id=str(value.get("state_id") or f"form:{form_name}:{page_index}"),
        label=str(value.get("label") or f"{form_name} 第 {page_index} 页"),
        page_index=page_index,
        total_pages=total_pages,
        form_name=form_name,
        fields=fields,
        synthetic_url=synthetic_url,
        url=url,
        submit=bool(value.get("submit", True)),
    )


def pagination_state_to_fetch_action(state: Any) -> dict[str, Any] | None:
    parsed = pagination_state_from_any(state)
    if parsed is None:
        return None
    return {
        "kind": parsed.kind,
        "form_name": parsed.form_name,
        "fields": dict(parsed.fields),
        "submit": parsed.submit,
        "synthetic_url": parsed.synthetic_url,
        "url": parsed.url,
        "label": parsed.label,
        "page_index": parsed.page_index,
        "state_id": parsed.state_id,
    }


class _FormPaginationParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.input_names: set[str] = set()
        self.current_pages: list[int] = []
        self._span_class_stack: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {str(key).lower(): str(value or "") for key, value in attrs}
        lowered = tag.lower()
        if lowered == "a":
            href = attr_map.get("href", "").strip()
            if href:
                self.hrefs.append(href)
        if lowered == "input":
            name = attr_map.get("name", "").strip()
            if name:
                self.input_names.add(name)
        if lowered == "span":
            self._span_class_stack.append(attr_map.get("class", "").lower())

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "span" and self._span_class_stack:
            self._span_class_stack.pop()

    def handle_data(self, data: str) -> None:
        if not self._span_class_stack:
            return
        current_class = self._span_class_stack[-1] or ""
        if "this-page" not in current_class:
            return
        value = _int_or_zero((data or "").strip())
        if value > 0:
            self.current_pages.append(value)


def _parse_absolute_page_assignment(href: str) -> tuple[str, str, int]:
    if not href:
        return "", "", 0
    if "javascript:" in href.lower():
        script = href.split(":", 1)[1]
    else:
        script = href
    match = _FORM_PAGE_ASSIGN_RE.search(script)
    if not match:
        return "", "", 0
    form = match.group("form").strip()
    field = match.group("field").strip()
    page = _int_or_zero(match.group("page"))
    submit = _FORM_SUBMIT_RE.search(script)
    if submit and submit.group("form") != form:
        return "", "", 0
    return form, field, page


def _matching_goto_field(input_names: set[str], page_field: str) -> str:
    for name in input_names:
        if not _GOTO_FIELD_RE.search(name):
            continue
        prefix = _GOTO_FIELD_RE.search(name).group("prefix") if _GOTO_FIELD_RE.search(name) else ""
        if not prefix or page_field.lower().startswith(prefix.lower()):
            return name
    return ""


def _current_page_from_parser(parser: _FormPaginationParser) -> int:
    return parser.current_pages[0] if parser.current_pages else 0


def _current_page_from_url(url: str) -> int:
    parsed = urlparse(url)
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {"pagenum", "page", "p", "pn", "fromwennowpage"}:
            page = _int_or_zero(value)
            if page > 0:
                return page
    return 0


def _int_or_zero(value: Any) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return 0


__all__ = [
    "FormPaginationState",
    "build_form_pagination_synthetic_url",
    "extract_form_pagination_states",
    "pagination_state_from_any",
    "pagination_state_to_fetch_action",
]
