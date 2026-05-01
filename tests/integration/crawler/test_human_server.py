"""Integration tests for the human-assisted HTTP API server."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer

from agents.crawler.fetchers.human_models import FetchJob, FetchJobStatus, JobContext, JobQueue
from agents.crawler.fetchers.human_server import create_app, _KEY_QUEUE


@pytest.fixture
async def client():
    queue = JobQueue()
    app = create_app(queue, agent_status_fn=lambda: {"state": "TESTING"})
    async with TestClient(TestServer(app)) as c:
        c.app_queue = queue  # type: ignore[attr-defined]
        yield c


async def _enqueue(client: TestClient, url: str = "https://example.edu.cn/") -> FetchJob:
    queue: JobQueue = client.app_queue  # type: ignore[attr-defined]
    job = FetchJob(url=url, context=JobContext(university_name="测试大学"))
    await queue.submit(job)
    return job


async def test_next_returns_204_when_empty(client: TestClient):
    resp = await client.get("/api/jobs/next")
    assert resp.status == 204


async def test_next_returns_job(client: TestClient):
    job = await _enqueue(client)
    resp = await client.get("/api/jobs/next")
    assert resp.status == 200
    data = await resp.json()
    assert data["id"] == job.id
    assert data["url"] == job.url
    assert data["context"]["university_name"] == "测试大学"


async def test_complete_job(client: TestClient):
    job = await _enqueue(client)
    await client.get("/api/jobs/next")  # assign

    resp = await client.post(f"/api/jobs/{job.id}/complete", json={
        "html": "<html><body>test</body></html>",
        "url": "https://example.edu.cn/page",
        "title": "Test Page",
    })
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "completed"
    assert job.status == FetchJobStatus.COMPLETED
    assert job.done_event.is_set()


async def test_complete_requires_html(client: TestClient):
    job = await _enqueue(client)
    await client.get("/api/jobs/next")

    resp = await client.post(f"/api/jobs/{job.id}/complete", json={"url": "x"})
    assert resp.status == 400


async def test_complete_returns_next_job(client: TestClient):
    j1 = await _enqueue(client, "https://a.edu.cn/")
    j2 = await _enqueue(client, "https://b.edu.cn/")
    await client.get("/api/jobs/next")  # assign j1

    resp = await client.post(f"/api/jobs/{j1.id}/complete", json={"html": "<html/>"})
    data = await resp.json()
    assert "next_job" in data
    assert data["next_job"]["id"] == j2.id


async def test_fail_job(client: TestClient):
    job = await _enqueue(client)
    await client.get("/api/jobs/next")

    resp = await client.post(f"/api/jobs/{job.id}/fail", json={"message": "login required"})
    assert resp.status == 200
    assert job.status == FetchJobStatus.FAILED
    assert job.error_message == "login required"


async def test_skip_job(client: TestClient):
    job = await _enqueue(client)
    await client.get("/api/jobs/next")

    resp = await client.post(f"/api/jobs/{job.id}/skip")
    assert resp.status == 200
    assert job.status == FetchJobStatus.SKIPPED


async def test_override_url(client: TestClient):
    job = await _enqueue(client)
    await client.get("/api/jobs/next")

    resp = await client.post(f"/api/jobs/{job.id}/override", json={"new_url": "https://new.edu.cn/"})
    assert resp.status == 200
    data = await resp.json()
    assert data["url"] == "https://new.edu.cn/"
    assert job.url == "https://new.edu.cn/"


async def test_override_requires_url(client: TestClient):
    job = await _enqueue(client)
    await client.get("/api/jobs/next")

    resp = await client.post(f"/api/jobs/{job.id}/override", json={})
    assert resp.status == 400


async def test_get_job(client: TestClient):
    job = await _enqueue(client)
    resp = await client.get(f"/api/jobs/{job.id}")
    assert resp.status == 200
    data = await resp.json()
    assert data["id"] == job.id


async def test_get_job_not_found(client: TestClient):
    resp = await client.get("/api/jobs/nonexistent")
    assert resp.status == 404


async def test_status_endpoint(client: TestClient):
    await _enqueue(client)
    resp = await client.get("/api/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["queue"]["pending"] == 1
    assert data["agent"]["state"] == "TESTING"
    assert "server_uptime_seconds" in data


async def test_cors_headers(client: TestClient):
    resp = await client.get("/api/status")
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


async def test_options_preflight(client: TestClient):
    resp = await client.options("/api/jobs/next")
    assert resp.status == 204
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"



async def test_status_includes_current_job(client: TestClient):
    job = await _enqueue(client)
    # Before assignment — no current_job
    resp = await client.get("/api/status")
    data = await resp.json()
    assert "current_job" not in data

    # Assign the job
    await client.get("/api/jobs/next")

    resp = await client.get("/api/status")
    data = await resp.json()
    assert data["current_job"]["id"] == job.id
    assert data["current_job"]["url"] == job.url

async def test_decision_get_and_resolve(client: TestClient):
    queue: JobQueue = client.app_queue  # type: ignore[attr-defined]
    decision = await queue.request_decision(
        kind="detail_fetch_failure",
        org_unit_name="CS",
        failure_count=10,
        sample_urls=["https://www.example.edu.cn/cs/teacher/1"],
    )

    resp = await client.get("/api/decision")
    assert resp.status == 200
    data = await resp.json()
    assert data["id"] == decision.id
    assert data["org_unit_name"] == "CS"

    resp = await client.post(f"/api/decision/{decision.id}/resolve", json={"action": "switch_failed_to_human"})
    assert resp.status == 200

    resp = await client.get("/api/decision")
    assert resp.status == 204


async def test_status_includes_pending_decision(client: TestClient):
    queue: JobQueue = client.app_queue  # type: ignore[attr-defined]
    await queue.request_decision(
        kind="detail_fetch_failure",
        org_unit_name="Math",
        failure_count=11,
        sample_urls=["https://www.example.edu.cn/math/teacher/2"],
    )

    resp = await client.get("/api/status")
    assert resp.status == 200
    data = await resp.json()
    assert data["pending_decision"]["org_unit_name"] == "Math"
