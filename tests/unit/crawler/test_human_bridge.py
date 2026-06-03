from __future__ import annotations

import asyncio

import pytest

from agents.crawler.fetchers import FetchResult
from agents.crawler.fetchers.human_bridge import HumanFetcherBridge
from agents.crawler.fetchers.human_models import FetchJob, FetchJobStatus, JobContext


async def test_fetch_returns_result_on_complete():
    bridge = HumanFetcherBridge(job_timeout_seconds=5)
    bridge.set_context(JobContext(university_name="测试大学"))

    async def _simulate_human():
        # Wait for job to appear
        for _ in range(50):
            job = await bridge.queue.next(timeout=0.05)
            if job:
                bridge.queue.complete(job.id, html="<html><body><a href='/page'>link</a></body></html>")
                return
            await asyncio.sleep(0.02)

    task = asyncio.create_task(_simulate_human())
    result = await bridge.fetch("https://example.edu.cn/")
    await task

    assert isinstance(result, FetchResult)
    assert result.status_code == 200
    assert result.text  # html2text output
    assert result.block_reason is None
    assert len(result.link_signals) == 1
    assert result.link_signals[0].url == "https://example.edu.cn/page"


async def test_fetch_returns_empty_on_skip():
    bridge = HumanFetcherBridge(job_timeout_seconds=5)

    async def _simulate_skip():
        for _ in range(50):
            job = await bridge.queue.next(timeout=0.05)
            if job:
                bridge.queue.skip(job.id)
                return
            await asyncio.sleep(0.02)

    task = asyncio.create_task(_simulate_skip())
    result = await bridge.fetch("https://example.edu.cn/")
    await task

    assert result.status_code == 0
    assert result.block_reason == "human_skip"
    assert result.text == ""


async def test_fetch_returns_empty_on_fail():
    bridge = HumanFetcherBridge(job_timeout_seconds=5)

    async def _simulate_fail():
        for _ in range(50):
            job = await bridge.queue.next(timeout=0.05)
            if job:
                bridge.queue.fail(job.id, "login required")
                return
            await asyncio.sleep(0.02)

    task = asyncio.create_task(_simulate_fail())
    result = await bridge.fetch("https://example.edu.cn/")
    await task

    assert result.status_code == 0
    assert result.block_reason == "login required"


async def test_fetch_rejects_invalid_url_without_queueing_job():
    bridge = HumanFetcherBridge(job_timeout_seconds=5)
    bad_url = (
        "https://example.edu.cn/szdw/"
        "%3Cspan%20style='color:red;font-size:9pt'%3E"
        "%E8%BD%AC%E6%8D%A2%E9%93%BE%E6%8E%A5%E9%94%99%E8%AF%AF%3C/span"
    )

    result = await bridge.fetch(bad_url)

    assert result.status_code == 0
    assert result.block_reason == "invalid_url"
    assert await bridge.queue.next(timeout=0.01) is None


async def test_fetch_timeout():
    bridge = HumanFetcherBridge(job_timeout_seconds=0.1)
    result = await bridge.fetch("https://example.edu.cn/")
    assert result.status_code == 0
    assert "timeout" in (result.block_reason or "")


async def test_set_context_propagates():
    bridge = HumanFetcherBridge(job_timeout_seconds=5)
    ctx = JobContext(university_name="北大", agent_state="EXTRACT_PROFESSORS", intent="提取教师")
    bridge.set_context(ctx)

    async def _check_and_complete():
        for _ in range(50):
            job = await bridge.queue.next(timeout=0.05)
            if job:
                assert job.context.university_name == "北大"
                assert job.context.intent == "提取教师"
                bridge.queue.complete(job.id, html="<html/>")
                return
            await asyncio.sleep(0.02)

    task = asyncio.create_task(_check_and_complete())
    await bridge.fetch("https://pku.edu.cn/")
    await task


async def test_filter_same_domain():
    links = ["https://cs.pku.edu.cn/a", "https://google.com/b", "https://math.pku.edu.cn/c"]
    result = HumanFetcherBridge.filter_same_domain(links, "https://www.pku.edu.cn/")
    assert "https://cs.pku.edu.cn/a" in result
    assert "https://math.pku.edu.cn/c" in result
    assert "https://google.com/b" not in result


async def test_queue_next_skips_stale_failed_job():
    bridge = HumanFetcherBridge(job_timeout_seconds=5)
    job = FetchJob(url="https://example.edu.cn/", context=JobContext())
    await bridge.queue.submit(job)
    bridge.queue.fail(job.id, "timeout")

    next_job = await bridge.queue.next(timeout=0.05)
    assert next_job is None


async def test_queue_complete_cannot_override_failed_job():
    bridge = HumanFetcherBridge(job_timeout_seconds=5)
    job = FetchJob(url="https://example.edu.cn/", context=JobContext())
    await bridge.queue.submit(job)
    bridge.queue.fail(job.id, "timeout")
    bridge.queue.complete(job.id, html="<html><body>late</body></html>", url="https://example.edu.cn/late")

    stored = bridge.queue.get(job.id)
    assert stored is not None
    assert stored.status == FetchJobStatus.FAILED
