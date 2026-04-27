from __future__ import annotations

import asyncio
import inspect
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from openai import AsyncOpenAI

from runtime.logger import get_logger


ToolHandler = Callable[..., Awaitable[Any] | Any]


@dataclass
class ToolCallRecord:
    name: str
    args: dict[str, Any]
    result: Any


@dataclass
class LLMResult:
    content: str
    tool_call_log: list[ToolCallRecord] = field(default_factory=list)


class LLMResponseError(RuntimeError):
    """Raised when an OpenAI-compatible endpoint returns an unexpected response."""


class LLMClient:
    """OpenAI Chat Completions client with a tool-calling loop.

    Note: Although the upstream gateway may support protocol conversion between
    Chat Completions and the Responses API, using Chat Completions here avoids
    conversion edge cases (e.g. empty assistant messages) that can cause some
    providers to hard-fail requests.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        max_rounds: int = 5,
        max_retries: int = 3,
        retry_base_delay: float = 5.0,
        max_concurrent: int = 2,
        min_interval: float = 1.0,
        timeout_seconds: float = 120.0,
        client: Any | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.max_rounds = max_rounds
        self.max_retries = max_retries
        self.retry_base_delay = retry_base_delay
        self.timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._min_interval = min_interval
        self._last_call_time: float = 0
        self._rate_lock = asyncio.Lock()
        self.client = client or AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout_seconds,
        )
        self.logger = get_logger("runtime.llm")

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_handlers: dict[str, ToolHandler] | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        working_messages: list[dict[str, Any]] = list(messages)
        handlers = tool_handlers or {}
        records: list[ToolCallRecord] = []
        last_content = ""

        for round_index in range(1, self.max_rounds + 1):
            self.logger.debug("LLM call round=%s model=%s", round_index, self.model)
            chat_tools = self._as_chat_tools(tools) if tools else None
            request: dict[str, Any] = {"model": self.model, "messages": working_messages}
            if chat_tools:
                request["tools"] = chat_tools
            if max_tokens is not None:
                request["max_tokens"] = max_tokens

            response = await self._call_with_retry(**request)
            message = self._extract_chat_message(response)
            content = (message.get("content") or "") if isinstance(message.get("content"), str) else ""
            last_content = content or last_content

            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return LLMResult(content=last_content, tool_call_log=records)

            # Append assistant message (including provider-specific fields such as reasoning_content).
            working_messages.append(message)

            for tool_call in tool_calls:
                call_id = tool_call.get("id") or f"call_{len(records) + 1}"
                function = tool_call.get("function") or {}
                name = function.get("name") or ""
                raw_args = function.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    self.logger.warning("Invalid JSON in tool arguments for %s: %s", name, str(raw_args)[:200])
                    working_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": '{"error": "Invalid JSON in tool arguments"}',
                        }
                    )
                    continue

                if name not in handlers:
                    raise KeyError(f"No handler registered for tool {name!r}")

                self.logger.debug("Executing tool name=%s args=%s", name, args)
                result = handlers[name](**args)
                if inspect.isawaitable(result):
                    result = await result

                records.append(ToolCallRecord(name=name, args=args, result=result))
                working_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": self._stringify_tool_result(result),
                    }
                )

        self.logger.warning("LLM tool loop stopped after max_rounds=%s", self.max_rounds)
        return LLMResult(content=last_content, tool_call_log=records)

    async def _call_with_retry(self, **request: Any) -> Any:
        _RETRYABLE_CODES = {429, 500, 502, 503, 529}
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            await self._rate_limit()
            try:
                async with self._semaphore:
                    return await self.client.chat.completions.create(**request)
            except Exception as error:
                last_error = error
                status = getattr(error, "status_code", None)
                retryable = status in _RETRYABLE_CODES or "timeout" in str(error).lower()
                if not retryable or attempt >= self.max_retries:
                    raise
                delay = self.retry_base_delay * (2 ** attempt)
                self.logger.warning(
                    "LLM call failed (attempt %s/%s, status=%s), retrying in %.1fs: %s",
                    attempt + 1, self.max_retries + 1, status, delay, error,
                )
                await asyncio.sleep(delay)
        raise last_error  # unreachable but satisfies type checker

    async def _rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_call_time)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_time = time.monotonic()

    def _extract_chat_message(self, response: Any) -> dict[str, Any]:
        choices = self._get(response, "choices") or []
        if not choices:
            raise LLMResponseError("Chat completion response missing choices")
        first = choices[0]
        message = self._get(first, "message")
        if message is None:
            raise LLMResponseError("Chat completion response missing message")
        if isinstance(message, dict):
            return message
        if hasattr(message, "model_dump"):
            return message.model_dump(exclude_none=True)
        # Best-effort coercion
        return {"role": getattr(message, "role", "assistant"), "content": getattr(message, "content", "")}

    def _as_chat_tools(self, tool_defs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        if not tool_defs:
            return []
        converted: list[dict[str, Any]] = []
        for tool in tool_defs:
            if tool.get("type") != "function":
                converted.append(tool)
                continue
            if "function" in tool:
                converted.append(tool)
                continue
            name = tool.get("name")
            if not name:
                converted.append(tool)
                continue
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": tool.get("description") or "",
                        "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                    },
                }
            )
        return converted

    def _stringify_tool_result(self, result: Any) -> str:
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    def _get(self, value: Any, key: str) -> Any:
        if isinstance(value, dict):
            return value.get(key)
        return getattr(value, key, None)
