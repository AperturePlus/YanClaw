from __future__ import annotations

import asyncio
import inspect
import json
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
    """Raised when an OpenAI-compatible endpoint returns a non-chat response."""


class LLMClient:
    """Small OpenAI-compatible chat client with a tool-calling loop."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        max_rounds: int = 5,
        max_retries: int = 3,
        retry_base_delay: float = 5.0,
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
        working_messages = [dict(message) for message in messages]
        handlers = tool_handlers or {}
        records: list[ToolCallRecord] = []
        last_content = ""

        for round_index in range(1, self.max_rounds + 1):
            self.logger.debug("LLM call round=%s model=%s", round_index, self.model)
            request: dict[str, Any] = {
                "model": self.model,
                "messages": working_messages,
                "tools": tools or None,
            }
            if max_tokens is not None:
                request["max_tokens"] = max_tokens
            response = await self._call_with_retry(**request)
            message = self._first_message(response)
            content = self._get(message, "content") or ""
            last_content = content
            tool_calls = self._get(message, "tool_calls") or []

            if not tool_calls:
                return LLMResult(content=content, tool_call_log=records)

            working_messages.append(
                {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": [self._tool_call_to_dict(tool_call) for tool_call in tool_calls],
                }
            )

            for tool_call in tool_calls:
                tool_call_id = self._get(tool_call, "id") or f"tool_{len(records) + 1}"
                function = self._get(tool_call, "function") or {}
                name = self._get(function, "name")
                raw_args = self._get(function, "arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                except json.JSONDecodeError:
                    self.logger.warning("Invalid JSON in tool arguments for %s: %s", name, raw_args[:200])
                    working_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call_id,
                            "name": name,
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
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "content": self._stringify_tool_result(result),
                    }
                )

        self.logger.warning("LLM tool loop stopped after max_rounds=%s", self.max_rounds)
        return LLMResult(content=last_content, tool_call_log=records)

    async def _call_with_retry(self, **request: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self.client.chat.completions.create(**request)
            except Exception as error:
                last_error = error
                retryable = "503" in str(error) or "429" in str(error) or "500" in str(error) or "busy" in str(error).lower() or "timeout" in str(error).lower()
                if not retryable or attempt >= self.max_retries:
                    raise
                delay = self.retry_base_delay * (2 ** attempt)
                self.logger.warning("LLM call failed (attempt %s/%s), retrying in %.1fs: %s", attempt + 1, self.max_retries + 1, delay, error)
                await asyncio.sleep(delay)
        raise last_error  # unreachable but satisfies type checker

    def _first_message(self, response: Any) -> Any:
        choices = self._get(response, "choices")
        if not isinstance(choices, list) or not choices:
            raise LLMResponseError(
                "LLM response did not include chat completion choices. "
                f"base_url={self.base_url!r} model={self.model!r} "
                f"response={self._summarize_response(response)}"
            )
        first_choice = choices[0]
        message = self._get(first_choice, "message")
        if message is None:
            raise LLMResponseError(
                "LLM response choice did not include a message. "
                f"base_url={self.base_url!r} model={self.model!r} "
                f"response={self._summarize_response(response)}"
            )
        return message

    def _tool_call_to_dict(self, tool_call: Any) -> dict[str, Any]:
        function = self._get(tool_call, "function") or {}
        return {
            "id": self._get(tool_call, "id"),
            "type": self._get(tool_call, "type") or "function",
            "function": {
                "name": self._get(function, "name"),
                "arguments": self._get(function, "arguments") or "{}",
            },
        }

    def _stringify_tool_result(self, result: Any) -> str:
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    def _get(self, value: Any, key: str) -> Any:
        if isinstance(value, dict):
            return value.get(key)
        return getattr(value, key, None)

    def _summarize_response(self, response: Any) -> str:
        if hasattr(response, "model_dump_json"):
            text = response.model_dump_json(exclude_none=True)
        elif hasattr(response, "model_dump"):
            text = json.dumps(response.model_dump(exclude_none=True), ensure_ascii=False, default=str)
        else:
            text = repr(response)
        return text[:2000]
