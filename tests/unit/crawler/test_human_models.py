from __future__ import annotations

import asyncio

import pytest

from agents.crawler.fetchers.human_models import (
    FetchJob,
    FetchJobStatus,
    JobContext,
    JobQueue,
)


def _make_job(url: str = "https://example.edu.cn/") -> FetchJob:
    return FetchJob(url=url, context=JobContext(university_name="测试大学"))


async def test_submit_and_next():
    q = JobQueue()
    job = _make_job()
    await q.submit(job)
    got = await q.next(timeout=1)
    assert got is not None
    assert got.id == job.id
    assert got.status == FetchJobStatus.ASSIGNED


async def test_next_returns_none_when_empty():
    q = JobQueue()
    assert await q.next(timeout=0.05) is None


async def test_complete_sets_result():
    q = JobQueue()
    job = _make_job()
    await q.submit(job)
    await q.next(timeout=1)

    q.complete(job.id, html="<html>ok</html>", url="https://example.edu.cn/page", title="Page")
    assert job.status == FetchJobStatus.COMPLETED
    assert job.result_html == "<html>ok</html>"
    assert job.result_url == "https://example.edu.cn/page"
    assert job.done_event.is_set()


async def test_fail_sets_message():
    q = JobQueue()
    job = _make_job()
    await q.submit(job)
    await q.next(timeout=1)

    q.fail(job.id, "page requires login")
    assert job.status == FetchJobStatus.FAILED
    assert job.error_message == "page requires login"
    assert job.done_event.is_set()


async def test_skip():
    q = JobQueue()
    job = _make_job()
    await q.submit(job)
    await q.next(timeout=1)

    q.skip(job.id)
    assert job.status == FetchJobStatus.SKIPPED
    assert job.done_event.is_set()


async def test_override_url():
    q = JobQueue()
    job = _make_job("https://old.edu.cn/")
    await q.submit(job)
    await q.next(timeout=1)

    q.override_url(job.id, "https://new.edu.cn/faculty")
    assert job.url == "https://new.edu.cn/faculty"


async def test_stats():
    q = JobQueue()
    j1 = _make_job("https://a.edu.cn/")
    j2 = _make_job("https://b.edu.cn/")
    j3 = _make_job("https://c.edu.cn/")
    await q.submit(j1)
    await q.submit(j2)
    await q.submit(j3)

    await q.next(timeout=1)  # j1 → assigned
    await q.next(timeout=1)  # j2 → assigned
    q.complete(j1.id, html="<html/>")
    q.fail(j2.id, "err")

    stats = q.stats()
    assert stats["pending"] == 1
    assert stats["completed"] == 1
    assert stats["failed"] == 1


async def test_require_raises_for_unknown_id():
    q = JobQueue()
    with pytest.raises(KeyError):
        q.complete("nonexistent", html="x")


async def test_done_event_unblocks_waiter():
    q = JobQueue()
    job = _make_job()
    await q.submit(job)
    await q.next(timeout=1)

    async def _complete_later():
        await asyncio.sleep(0.05)
        q.complete(job.id, html="<html/>")

    asyncio.create_task(_complete_later())
    await asyncio.wait_for(job.done_event.wait(), timeout=2)
    assert job.status == FetchJobStatus.COMPLETED


def test_job_to_dict():
    ctx = JobContext(university_name="北大", agent_state="FIND_FACULTY_PAGES", intent="找师资")
    job = FetchJob(url="https://pku.edu.cn/", context=ctx)
    d = job.to_dict()
    assert d["url"] == "https://pku.edu.cn/"
    assert d["context"]["university_name"] == "北大"
    assert d["context"]["intent"] == "找师资"
    assert d["status"] == "pending"



async def test_current_assigned_returns_active_job():
    q = JobQueue()
    j1 = _make_job("https://a.edu.cn/")
    j2 = _make_job("https://b.edu.cn/")
    await q.submit(j1)
    await q.submit(j2)

    assert q.current_assigned() is None  # nothing assigned yet

    await q.next(timeout=1)  # j1 → assigned
    assert q.current_assigned() is not None
    assert q.current_assigned().id == j1.id

    q.complete(j1.id, html="<html/>")
    # j1 completed, j2 still pending
    assert q.current_assigned() is None

    await q.next(timeout=1)  # j2 → assigned
    assert q.current_assigned().id == j2.id

async def test_decision_request_wait_and_resolve():
    q = JobQueue()
    decision = await q.request_decision(
        kind="detail_fetch_failure",
        org_unit_name="CS",
        failure_count=10,
        sample_urls=["https://www.example.edu.cn/cs/teacher/1"],
    )
    assert q.pending_decision() is not None
    assert q.pending_decision().id == decision.id

    q.resolve_decision(decision.id, "switch_failed_to_human")
    resolved = await q.wait_decision(decision.id, timeout=1)
    assert resolved.status.value == "resolved"
    assert resolved.action == "switch_failed_to_human"
    assert q.pending_decision() is None
