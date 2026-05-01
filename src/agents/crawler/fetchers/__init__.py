"""Fetcher backends for the crawler agent.

Public API re-exports so consumers can write::

    from agents.crawler.fetchers import Fetcher, FetchResult, HumanFetcherBridge
"""

from agents.crawler.fetchers.httpx_fetcher import (
    FetchResult,
    Fetcher,
    _detect_block_reason,
    _is_ssl_error,
    _site_root,
)
from agents.crawler.fetchers.human_bridge import HumanFetcherBridge
from agents.crawler.fetchers.human_models import (
    DecisionRequest,
    DecisionStatus,
    FetchJob,
    FetchJobStatus,
    JobContext,
    JobQueue,
)
from agents.crawler.fetchers.human_server import create_app

__all__ = [
    "FetchResult",
    "Fetcher",
    "HumanFetcherBridge",
    "FetchJob",
    "FetchJobStatus",
    "DecisionRequest",
    "DecisionStatus",
    "JobContext",
    "JobQueue",
    "create_app",
    "_detect_block_reason",
    "_is_ssl_error",
    "_site_root",
]
