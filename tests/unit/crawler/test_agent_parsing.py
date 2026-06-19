from __future__ import annotations

from types import SimpleNamespace

from agents.crawler import agent_parsing
from agents.crawler.fetchers.httpx_fetcher import Fetcher


def _pagination_harness(start_url: str = "https://www.sjtu.edu.cn/") -> SimpleNamespace:
    """Minimal stand-in for the crawler agent: extract_pagination_links only
    touches .fetcher.filter_same_domain, .start_url and .visited_urls."""
    return SimpleNamespace(
        fetcher=SimpleNamespace(filter_same_domain=Fetcher.filter_same_domain),
        start_url=start_url,
        visited_urls=set(),
    )


def test_pagination_links_reject_cross_host_sibling_subdomain() -> None:
    """Regression: a pagination-shaped URL on a *different* host than the page
    being paginated must not be accepted as pagination.

    The SEIEE faculty list lives on www.seiee.sjtu.edu.cn; the 工程教育中心 news
    microsite lives on mem.seiee.sjtu.edu.cn. Both collapse to the same
    registrable domain (sjtu.edu.cn) and the news URL is pagination-shaped
    (-\\d+.htm), so the old code enqueued it as pagination_of the faculty list.
    Fetching it spilled the whole microsite into the frontier (251 nodes, 0
    professors).
    """
    harness = _pagination_harness()
    current = "https://www.seiee.sjtu.edu.cn/szdw.html"
    cross_host = "https://mem.seiee.sjtu.edu.cn/mem/list/11-1-20.htm"

    result = agent_parsing.extract_pagination_links(harness, [cross_host], current)

    assert cross_host not in result
    assert result == []


def test_pagination_links_keep_same_host_pagination() -> None:
    """The same-host guard must not over-filter genuine pagination of the list."""
    harness = _pagination_harness()
    current = "https://www.seiee.sjtu.edu.cn/szdw.html"
    same_host = "https://www.seiee.sjtu.edu.cn/szdw/list_2.htm"

    result = agent_parsing.extract_pagination_links(harness, [same_host], current)

    assert same_host in result
