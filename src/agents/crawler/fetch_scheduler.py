from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from agents.crawler import db as crawler_db
from agents.crawler.fetchers import FetchResult
from agents.crawler.models import CrawlLogStatus
from agents.crawler.url_heuristics import _same_site, _sanitize_url


class FetchScheduler:
    """Fetch/cache/dedup boundary for a single crawler agent."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    async def fetch_url(self, url: str, depth: int) -> FetchResult | None:
        agent = self.agent
        url = _sanitize_url(url)
        if not url:
            return None
        if not agent._within_depth(depth):
            agent.execution_log.append(f"skip depth url={url} depth={depth}")
            agent.logger.info("Skipping %s: depth %s exceeds max_depth=%s", url, depth, agent.max_depth)
            return None
        if not _same_site(url, agent.start_url):
            agent.execution_log.append(f"skip external url={url}")
            agent.logger.info("Skipping external URL: %s", url)
            return None

        if url in agent.visited_urls and not agent._skip_cross_run_dedup:
            cached = agent._fetch_cache.get(url)
            if cached is not None and url == _sanitize_url(agent.start_url):
                agent.execution_log.append(f"fetch cache url={url} depth={depth}")
                agent.logger.debug("Using cached start URL: %s", url)
                return cached
            agent.execution_log.append(f"skip visited url={url}")
            agent.logger.info("Skipping already visited URL: %s", url)
            return None

        cached = agent._fetch_cache.get(url)
        if cached is not None:
            agent.execution_log.append(f"fetch cache url={url} depth={depth}")
            agent.logger.debug("Using cached URL: %s", url)
            return cached

        if url != agent.start_url and not agent._skip_cross_run_dedup:
            async with agent.db.session() as session:
                if await crawler_db.is_url_crawled(session, url):
                    agent.visited_urls.add(url)
                    agent.execution_log.append(f"skip already_crawled url={url}")
                    agent.logger.info("Skipping previously crawled URL: %s", url)
                    return None

        agent.visited_urls.add(url)
        try:
            fetched = await agent.fetcher.fetch(url)
        except Exception as error:
            async with agent.db.session() as session:
                await crawler_db.log_crawl(
                    session,
                    url,
                    CrawlLogStatus.FAILED,
                    str(error),
                )
            agent.execution_log.append(f"fetch failed url={url} error={error}")
            agent.logger.warning("Fetch failed for %s: %s", url, error)
            return None

        canonical = _sanitize_url(fetched.url)
        if canonical:
            agent.visited_urls.add(canonical)
            agent._fetch_cache.setdefault(canonical, fetched)
        agent._fetch_cache.setdefault(url, fetched)

        async with agent.db.session() as session:
            crawl_status = CrawlLogStatus.SUCCESS
            crawl_message = f"depth={depth} status_code={fetched.status_code}"
            if fetched.block_reason:
                crawl_status = CrawlLogStatus.FAILED
                crawl_message = f"{crawl_message} blocked={fetched.block_reason} links={len(fetched.links)}"
            await crawler_db.log_crawl(
                session,
                fetched.url,
                crawl_status,
                crawl_message,
            )

        if fetched.block_reason:
            blocked_host = (urlparse(fetched.url).hostname or "").lower()
            if blocked_host:
                agent._blocked_hosts.add(blocked_host)
            agent.logger.warning(
                "WAF/challenge page detected url=%s status=%s reason=%s links=%s",
                fetched.url,
                fetched.status_code,
                fetched.block_reason,
                len(fetched.links),
            )
            agent.execution_log.append(
                f"fetch blocked url={fetched.url} depth={depth} status={fetched.status_code} reason={fetched.block_reason}"
            )
        else:
            agent.execution_log.append(
                f"fetch ok url={fetched.url} depth={depth} status={fetched.status_code} links={len(fetched.links)}"
            )
        return fetched
