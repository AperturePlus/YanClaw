from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from agents.crawler import db as crawler_db
from agents.crawler.fetch_failures import FetchFailureKind, classify_fetch_failure
from agents.crawler.fetchers import FetchResult
from agents.crawler.models import CrawlLogStatus
from agents.crawler.url_heuristics import _same_site, _sanitize_url
from agents.crawler.url_validation import normalize_crawlable_url


class FetchScheduler:
    """Fetch/cache/dedup boundary for a single crawler agent."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    async def fetch_url(
        self,
        url: str,
        depth: int,
        *,
        action: dict[str, Any] | None = None,
        identity_url: str | None = None,
        allow_depth_excess: bool = False,
    ) -> FetchResult | None:
        agent = self.agent
        url = _sanitize_url(url)
        if not url:
            return None
        identity = _sanitize_url(identity_url or "") or None
        normalized_url = normalize_crawlable_url(url)
        if not normalized_url:
            stats = getattr(agent, "_pipeline_stats", None)
            if isinstance(stats, dict):
                stats["invalid_urls_skipped"] = int(stats.get("invalid_urls_skipped", 0)) + 1
            agent.execution_log.append(f"skip invalid_url url={url}")
            agent.logger.warning("Skipping invalid URL before fetch: %s", url)
            return None
        url = normalized_url
        if not agent._within_depth(depth) and not allow_depth_excess:
            agent.execution_log.append(f"skip depth url={url} depth={depth}")
            agent.logger.info("Skipping %s: depth %s exceeds max_depth=%s", url, depth, agent.max_depth)
            return None
        if not _same_site(url, agent.start_url):
            agent.execution_log.append(f"skip external url={url}")
            agent.logger.info("Skipping external URL: %s", url)
            return None

        resume_mode = bool(getattr(agent, "resume_mode", False))
        cross_run_dedup_enabled = resume_mode or not agent._skip_cross_run_dedup
        force_refetch_urls = getattr(agent, "_resume_force_refetch_urls", set())
        dedup_url = identity or url
        force_refetch = dedup_url in force_refetch_urls or dedup_url.rstrip("/") in force_refetch_urls
        if force_refetch:
            agent.execution_log.append(f"force refetch url={dedup_url}")

        if resume_mode and not force_refetch:
            async with agent.db.session() as session:
                cached = await crawler_db.get_cached_fetch_result(session, dedup_url)
            if cached is not None and not cached.block_reason:
                canonical = _sanitize_url(cached.url)
                agent.visited_urls.add(dedup_url)
                if canonical:
                    agent.visited_urls.add(canonical)
                    agent._fetch_cache.setdefault(canonical, cached)
                agent._fetch_cache.setdefault(dedup_url, cached)
                agent.execution_log.append(f"fetch resume_cache url={dedup_url} depth={depth}")
                agent.logger.info("Using cached resume page: %s", dedup_url)
                return cached
            if cached is not None and cached.block_reason:
                agent.logger.info(
                    "Ignoring failed cached page in resume so it can be retried: %s reason=%s",
                    url,
                    cached.block_reason,
                )

        if dedup_url in agent.visited_urls and cross_run_dedup_enabled and not force_refetch:
            cached = agent._fetch_cache.get(dedup_url)
            if cached is not None and dedup_url == _sanitize_url(agent.start_url):
                agent.execution_log.append(f"fetch cache url={dedup_url} depth={depth}")
                agent.logger.debug("Using cached start URL: %s", dedup_url)
                return cached
            agent.execution_log.append(f"skip visited url={dedup_url}")
            agent.logger.info("Skipping already visited URL: %s", dedup_url)
            return None

        cached = agent._fetch_cache.get(dedup_url)
        if cached is not None and not force_refetch:
            agent.execution_log.append(f"fetch cache url={dedup_url} depth={depth}")
            agent.logger.debug("Using cached URL: %s", dedup_url)
            return cached

        if cross_run_dedup_enabled and not force_refetch and (resume_mode or dedup_url != agent.start_url):
            async with agent.db.session() as session:
                if await crawler_db.is_url_crawled(session, dedup_url):
                    agent.visited_urls.add(dedup_url)
                    agent.execution_log.append(f"skip already_crawled url={dedup_url}")
                    agent.logger.info("Skipping previously crawled URL: %s", dedup_url)
                    return None

        agent.visited_urls.add(dedup_url)
        if force_refetch:
            try:
                force_refetch_urls.discard(dedup_url)
                force_refetch_urls.discard(dedup_url.rstrip("/"))
            except AttributeError:
                pass
        try:
            if action is not None:
                fetched = await agent.fetcher.fetch(url, action=action, identity_url=identity)
            else:
                fetched = await agent.fetcher.fetch(url)
        except Exception as error:
            async with agent.db.session() as session:
                await crawler_db.log_crawl(
                    session,
                    dedup_url,
                    CrawlLogStatus.FAILED,
                    str(error),
                )
            agent.execution_log.append(f"fetch failed url={dedup_url} error={error}")
            agent.logger.warning("Fetch failed for %s: %s", dedup_url, error)
            return None

        canonical = _sanitize_url(fetched.url) or dedup_url
        if canonical:
            agent.visited_urls.add(canonical)
            agent._fetch_cache.setdefault(canonical, fetched)
        agent._fetch_cache.setdefault(dedup_url, fetched)

        async with agent.db.session() as session:
            await crawler_db.upsert_page_cache(session, url=dedup_url, fetched=fetched)
            if canonical and canonical != dedup_url:
                await crawler_db.upsert_page_cache(session, url=canonical, fetched=fetched)
            crawl_status = CrawlLogStatus.SUCCESS
            crawl_message = f"depth={depth} status_code={fetched.status_code}"
            if fetched.block_reason:
                crawl_status = CrawlLogStatus.FAILED
                failure_kind = classify_fetch_failure(fetched.block_reason)
                if failure_kind == FetchFailureKind.BLOCKED:
                    crawl_message = f"{crawl_message} blocked={fetched.block_reason} links={len(fetched.links)}"
                else:
                    crawl_message = (
                        f"{crawl_message} fetch_failure={fetched.block_reason} links={len(fetched.links)}"
                    )
            await crawler_db.log_crawl(
                session,
                canonical or dedup_url,
                crawl_status,
                crawl_message,
            )
            if canonical and canonical != dedup_url:
                await crawler_db.log_crawl(
                    session,
                    dedup_url,
                    crawl_status,
                    f"{crawl_message} final_url={fetched.url}",
                )

        if fetched.block_reason:
            failure_kind = classify_fetch_failure(fetched.block_reason)
            if failure_kind == FetchFailureKind.BLOCKED:
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
            elif failure_kind == FetchFailureKind.RETRYABLE:
                if (fetched.block_reason or "").strip().lower() == "timeout":
                    agent.logger.warning(
                        "Human fetch timed out url=%s status=%s links=%s",
                        fetched.url,
                        fetched.status_code,
                        len(fetched.links),
                    )
                else:
                    agent.logger.warning(
                        "Fetch failed with retryable reason url=%s status=%s reason=%s links=%s",
                        fetched.url,
                        fetched.status_code,
                        fetched.block_reason,
                        len(fetched.links),
                    )
                agent.execution_log.append(
                    f"fetch retryable_failure url={fetched.url} depth={depth} status={fetched.status_code} reason={fetched.block_reason}"
                )
            else:
                agent.logger.warning(
                    "Fetch failed url=%s status=%s reason=%s links=%s",
                    fetched.url,
                    fetched.status_code,
                    fetched.block_reason,
                    len(fetched.links),
                )
                agent.execution_log.append(
                    f"fetch failed url={fetched.url} depth={depth} status={fetched.status_code} reason={fetched.block_reason}"
                )
        else:
            agent.execution_log.append(
                f"fetch ok url={fetched.url} depth={depth} status={fetched.status_code} links={len(fetched.links)}"
            )
        return fetched
