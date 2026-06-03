from __future__ import annotations

import json
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler.fetchers import FetchResult
from agents.crawler.fetchers.link_signals import LinkSignal
from agents.crawler.models import CrawlPageCache
from agents.crawler.db.utils import _normalize_url, _now_utc


async def upsert_page_cache(
    session: AsyncSession,
    *,
    url: str,
    fetched: FetchResult,
) -> CrawlPageCache | None:
    cache_url = _normalize_url(url)
    final_url = _normalize_url(fetched.url) or cache_url
    if not cache_url:
        return None

    values = {
        "final_url": final_url,
        "status_code": int(fetched.status_code or 0),
        "text_snapshot": fetched.text or "",
        "links_json": json.dumps(list(fetched.links or []), ensure_ascii=False),
        "link_signals_json": json.dumps(
            [_serialize_link_signal(signal) for signal in (fetched.link_signals or ())],
            ensure_ascii=False,
        ),
        "block_reason": fetched.block_reason,
    }
    row = (
        await session.execute(
            select(CrawlPageCache)
            .where(CrawlPageCache.url == cache_url)
            .order_by(CrawlPageCache.id.asc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        row = CrawlPageCache(
            url=cache_url,
            **values,
            created_at=_now_utc(),
            updated_at=_now_utc(),
        )
        session.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = _now_utc()
    await session.flush()
    return row


async def get_cached_fetch_result(session: AsyncSession, url: str) -> FetchResult | None:
    normalized = _normalize_url(url)
    if not normalized:
        return None
    row = (
        await session.execute(
            select(CrawlPageCache)
            .where(or_(CrawlPageCache.url == normalized, CrawlPageCache.final_url == normalized))
            .order_by((CrawlPageCache.url == normalized).desc(), CrawlPageCache.id.asc())
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return None

    links = _json_list(row.links_json)
    link_signals = tuple(_deserialize_link_signal(item) for item in _json_list(row.link_signals_json, dict_only=True))
    return FetchResult(
        url=row.final_url or row.url,
        text=row.text_snapshot or "",
        links=[str(item) for item in links if isinstance(item, str)],
        status_code=int(row.status_code or 0),
        block_reason=row.block_reason,
        link_signals=link_signals,
    )


def _serialize_link_signal(signal: Any) -> dict[str, Any]:
    return {
        "url": str(getattr(signal, "url", "") or ""),
        "anchor_text": str(getattr(signal, "anchor_text", "") or ""),
        "heading_text": str(getattr(signal, "heading_text", "") or ""),
        "parent_tags_or_classes": [
            str(item) for item in (getattr(signal, "parent_tags_or_classes", ()) or ())
        ],
        "link_order": int(getattr(signal, "link_order", 0) or 0),
    }


def _deserialize_link_signal(item: Any) -> LinkSignal:
    if not isinstance(item, dict):
        return LinkSignal(url="")
    parent = item.get("parent_tags_or_classes") or ()
    if not isinstance(parent, (list, tuple)):
        parent = ()
    return LinkSignal(
        url=str(item.get("url") or ""),
        anchor_text=str(item.get("anchor_text") or ""),
        heading_text=str(item.get("heading_text") or ""),
        parent_tags_or_classes=tuple(str(value) for value in parent),
        link_order=int(item.get("link_order") or 0),
    )


def _json_list(value: Any, *, dict_only: bool = False) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(str(value))
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    if dict_only:
        return [item for item in parsed if isinstance(item, dict)]
    return parsed


__all__ = [
    "get_cached_fetch_result",
    "upsert_page_cache",
]
