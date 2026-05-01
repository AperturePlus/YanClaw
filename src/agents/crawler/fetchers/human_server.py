"""aiohttp HTTP server exposing the human-assisted job queue API."""

from __future__ import annotations

import json
import time
from typing import Any

from aiohttp import web

from agents.crawler.fetchers.human_models import FetchJobStatus, JobQueue
from runtime.logger import get_logger

logger = get_logger("crawler.human_server")

_KEY_QUEUE = web.AppKey("queue", JobQueue)
_KEY_AGENT_STATUS = web.AppKey("agent_status_fn", object)
_KEY_START_TIME = web.AppKey("start_time", float)


def create_app(queue: JobQueue, agent_status_fn: Any = None) -> web.Application:
    """Build the aiohttp app wired to *queue*."""
    app = web.Application(middlewares=[_cors_middleware])
    app[_KEY_QUEUE] = queue
    app[_KEY_AGENT_STATUS] = agent_status_fn
    app[_KEY_START_TIME] = time.monotonic()
    app.router.add_get("/api/jobs/next", _handle_next)
    app.router.add_get("/api/jobs/{id}", _handle_get_job)
    app.router.add_post("/api/jobs/{id}/complete", _handle_complete)
    app.router.add_post("/api/jobs/{id}/fail", _handle_fail)
    app.router.add_post("/api/jobs/{id}/skip", _handle_skip)
    app.router.add_post("/api/jobs/{id}/override", _handle_override)
    app.router.add_get("/api/decision", _handle_get_decision)
    app.router.add_post("/api/decision/{id}/resolve", _handle_resolve_decision)
    app.router.add_get("/api/status", _handle_status)
    # Preflight
    app.router.add_route("OPTIONS", "/{path:.*}", _handle_options)
    return app


@web.middleware
async def _cors_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


async def _handle_options(_request: web.Request) -> web.Response:
    return web.Response(status=204)


async def _handle_next(request: web.Request) -> web.Response:
    queue: JobQueue = request.app[_KEY_QUEUE]
    job = await queue.next(timeout=0.1)
    if job is None:
        return web.Response(status=204)
    logger.info("Job assigned id=%s url=%s", job.id, job.url)
    return _json_response(job.to_dict())


async def _handle_get_job(request: web.Request) -> web.Response:
    queue: JobQueue = request.app[_KEY_QUEUE]
    job = queue.get(request.match_info["id"])
    if job is None:
        return _json_response({"error": "not found"}, status=404)
    return _json_response(job.to_dict())


async def _handle_complete(request: web.Request) -> web.Response:
    queue: JobQueue = request.app[_KEY_QUEUE]
    job_id = request.match_info["id"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception):
        return _json_response({"error": "invalid json"}, status=400)

    html = body.get("html", "")
    if not html:
        return _json_response({"error": "html is required"}, status=400)

    try:
        job = queue.complete(
            job_id,
            html=html,
            url=body.get("url"),
            title=body.get("title"),
        )
    except KeyError:
        return _json_response({"error": "not found"}, status=404)

    logger.info("Job completed id=%s url=%s", job.id, job.result_url)

    # Eagerly return next job to reduce round-trips.
    next_job = await queue.next(timeout=0.1)
    result: dict[str, Any] = {"status": "completed"}
    if next_job is not None:
        result["next_job"] = next_job.to_dict()
    return _json_response(result)


async def _handle_fail(request: web.Request) -> web.Response:
    queue: JobQueue = request.app[_KEY_QUEUE]
    job_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        job = queue.fail(job_id, body.get("message", ""))
    except KeyError:
        return _json_response({"error": "not found"}, status=404)
    logger.info("Job failed id=%s message=%s", job.id, job.error_message)
    return _json_response({"status": "failed"})


async def _handle_skip(request: web.Request) -> web.Response:
    queue: JobQueue = request.app[_KEY_QUEUE]
    try:
        queue.skip(request.match_info["id"])
    except KeyError:
        return _json_response({"error": "not found"}, status=404)
    return _json_response({"status": "skipped"})


async def _handle_override(request: web.Request) -> web.Response:
    queue: JobQueue = request.app[_KEY_QUEUE]
    try:
        body = await request.json()
    except Exception:
        return _json_response({"error": "invalid json"}, status=400)
    new_url = body.get("new_url", "").strip()
    if not new_url:
        return _json_response({"error": "new_url is required"}, status=400)
    try:
        job = queue.override_url(request.match_info["id"], new_url)
    except KeyError:
        return _json_response({"error": "not found"}, status=404)
    logger.info("Job URL overridden id=%s new_url=%s", job.id, new_url)
    return _json_response(job.to_dict())


async def _handle_get_decision(request: web.Request) -> web.Response:
    queue: JobQueue = request.app[_KEY_QUEUE]
    decision = queue.pending_decision()
    if decision is None:
        return web.Response(status=204)
    return _json_response(decision.to_dict())


async def _handle_resolve_decision(request: web.Request) -> web.Response:
    queue: JobQueue = request.app[_KEY_QUEUE]
    decision_id = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    action = str(body.get("action", "")).strip()
    if not action:
        return _json_response({"error": "action is required"}, status=400)
    try:
        decision = queue.resolve_decision(decision_id, action)
    except KeyError:
        return _json_response({"error": "not found"}, status=404)
    logger.info("Decision resolved id=%s action=%s", decision.id, action)
    return _json_response({"status": "resolved", "decision": decision.to_dict()})


async def _handle_status(request: web.Request) -> web.Response:
    queue: JobQueue = request.app[_KEY_QUEUE]
    status_fn = request.app[_KEY_AGENT_STATUS]
    data: dict[str, Any] = {
        "queue": queue.stats(),
        "server_uptime_seconds": round(time.monotonic() - request.app[_KEY_START_TIME], 1),
    }
    assigned = queue.current_assigned()
    if assigned is not None:
        data["current_job"] = assigned.to_dict()
    pending_decision = queue.pending_decision()
    if pending_decision is not None:
        data["pending_decision"] = pending_decision.to_dict()
    if status_fn is not None:
        try:
            data["agent"] = status_fn()
        except Exception:
            pass
    return _json_response(data)


def _json_response(data: Any, *, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)
