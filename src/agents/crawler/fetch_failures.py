"""Classification helpers for fetch-layer failure reasons."""

from __future__ import annotations

from enum import Enum


class FetchFailureKind(str, Enum):
    BLOCKED = "blocked"
    RETRYABLE = "retryable"
    TERMINAL = "terminal"


_BLOCKED_MARKERS = (
    "waf",
    "challenge",
    "captcha",
    "security check",
    "web application firewall",
    "bot detection",
    "正在验证",
    "安全验证",
    "人机验证",
)

_RETRYABLE_MARKERS = (
    "timeout",
    "timed out",
    "human_failed",
    "network",
    "connection",
    "temporarily",
    "temporary",
    "reset",
    "refused",
    "unreachable",
)

_TERMINAL_REASONS = {
    "human_skip",
    "invalid_url",
}


def classify_fetch_failure(reason: str | None) -> FetchFailureKind | None:
    normalized = (reason or "").strip().lower()
    if not normalized:
        return None
    if normalized in _TERMINAL_REASONS:
        return FetchFailureKind.TERMINAL
    if any(marker in normalized for marker in _BLOCKED_MARKERS):
        return FetchFailureKind.BLOCKED
    if normalized == "timeout" or any(marker in normalized for marker in _RETRYABLE_MARKERS):
        return FetchFailureKind.RETRYABLE
    return FetchFailureKind.TERMINAL


def is_blocking_fetch_failure(reason: str | None) -> bool:
    return classify_fetch_failure(reason) == FetchFailureKind.BLOCKED


def is_retryable_fetch_failure(reason: str | None) -> bool:
    return classify_fetch_failure(reason) == FetchFailureKind.RETRYABLE
