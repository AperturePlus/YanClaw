from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urldefrag, urlparse, urlunparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _serialize_optional(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _clean_url(value: Any) -> str:
    if value is None:
        return ""
    text_value = str(value).strip().strip('"').strip("'").strip()
    if text_value.startswith("<"):
        text_value = text_value[1:]
    if text_value.endswith(">"):
        text_value = text_value[:-1]
    text_value = text_value.strip()
    if text_value.startswith("[") and "](" in text_value and text_value.endswith(")"):
        text_value = text_value.split("](", 1)[1][:-1].strip()
    return text_value


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().strip("<>").strip()


def _normalize_email(value: Any) -> str | None:
    if value is None:
        return None
    email = str(value).strip().lower()
    return email or None


def _normalize_homepage(value: Any) -> str | None:
    if value is None:
        return None
    homepage = str(value).strip()
    if not homepage:
        return None
    homepage = urldefrag(homepage)[0]
    parsed = urlparse(homepage)
    if not parsed.scheme or not parsed.netloc:
        return homepage.rstrip("/")
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip("/"),
    )
    return urlunparse(normalized)


def _normalize_url(value: Any) -> str:
    text_value = _clean_url(value)
    if not text_value:
        return ""
    text_value = urldefrag(text_value)[0]
    parsed = urlparse(text_value)
    if not parsed.scheme or not parsed.netloc:
        return text_value.rstrip("/")
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip("/"),
    )
    return urlunparse(normalized)


def _site_root(host: str) -> str:
    parts = [part for part in host.split(".") if part]
    if len(parts) >= 3 and parts[-1] == "cn" and parts[-2] in {"edu", "ac", "com", "net", "org", "gov"}:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def _site_root_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return _site_root(host)


def _merge_org_unit_names(current: Any, incoming: Any) -> str:
    current_text = str(current or "").strip()
    incoming_text = str(incoming or "").strip()
    if not current_text:
        return incoming_text
    if not incoming_text or current_text == incoming_text:
        return current_text
    current_parts = [part.strip() for part in current_text.split(" / ") if part.strip()]
    incoming_parts = [part.strip() for part in incoming_text.split(" / ") if part.strip()]
    merged: list[str] = []
    for part in [*current_parts, *incoming_parts]:
        if part not in merged:
            merged.append(part)
    return " / ".join(merged)


def _should_replace_org_unit_url(current: str, candidate: str) -> bool:
    current_clean = _normalize_url(current)
    candidate_clean = _normalize_url(candidate)
    if not candidate_clean:
        return False
    if not current_clean:
        return True
    current_score = 0
    candidate_score = 0
    if "/info/" in current_clean:
        current_score += 4
    if "/info/" in candidate_clean:
        candidate_score += 4
    current_depth = max(0, urlparse(current_clean).path.count("/") - 1)
    candidate_depth = max(0, urlparse(candidate_clean).path.count("/") - 1)
    current_score += current_depth
    candidate_score += candidate_depth
    return candidate_score > current_score


async def _sqlite_has_column(session: AsyncSession, table: str, column: str) -> bool:
    result = await session.execute(text(f"PRAGMA table_info({table})"))
    rows = result.fetchall()
    for row in rows:
        if len(row) > 1 and str(row[1]) == column:
            return True
    return False


async def _normalize_nullish_columns(
    session: AsyncSession,
    table: str,
    columns: list[str],
) -> None:
    for column in columns:
        await session.execute(
            text(
                f"UPDATE {table} SET {column} = NULL "
                f"WHERE {column} IS NOT NULL AND ("
                f"trim({column}) = '' OR lower(trim({column})) IN "
                "('null', 'none', 'n/a', 'na', 'nan', '--', '-', 'unknown')"
                ")"
            )
        )


def _row_value(row: dict[str, Any], *keys: str, fallback_index: int | None = None) -> str:
    normalized = {_normalize_header(key): value for key, value in row.items()}
    for key in keys:
        value = normalized.get(_normalize_header(key))
        if value:
            return str(value)
    if fallback_index is not None:
        values = list(row.values())
        if fallback_index < len(values) and values[fallback_index] is not None:
            return str(values[fallback_index])
    return ""


def _normalize_header(value: Any) -> str:
    return str(value).strip().lower().replace(" ", "_")


def _now_utc() -> Any:
    return datetime.now(timezone.utc)

