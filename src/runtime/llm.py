from __future__ import annotations

import asyncio
import inspect
import json
import re
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
class ToolCallErrorRecord:
    name: str
    raw_args_preview: str
    error_type: str


@dataclass
class LLMResult:
    content: str
    tool_call_log: list[ToolCallRecord] = field(default_factory=list)
    invalid_tool_calls: list[ToolCallErrorRecord] = field(default_factory=list)


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
        invalid_calls: list[ToolCallErrorRecord] = []
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
            tool_names = [
                str((tool_call.get("function") or {}).get("name") or "")
                for tool_call in tool_calls
                if isinstance(tool_call, dict)
            ]
            self.logger.debug(
                "LLM response round=%s content_chars=%s tool_calls=%s",
                round_index,
                len(content),
                ",".join(name for name in tool_names if name) or "none",
            )
            if content:
                self.logger.debug("LLM response preview: %s", self._preview_text(content))
            if not tool_calls:
                return LLMResult(
                    content=last_content,
                    tool_call_log=records,
                    invalid_tool_calls=invalid_calls,
                )

            # Append assistant message (including provider-specific fields such as reasoning_content).
            working_messages.append(message)

            for tool_call in tool_calls:
                call_id = tool_call.get("id") or f"call_{len(records) + 1}"
                function = tool_call.get("function") or {}
                name = function.get("name") or ""
                raw_args = function.get("arguments") or "{}"
                args = self._parse_tool_arguments(raw_args)
                if args is None:
                    self.logger.warning("Invalid JSON in tool arguments for %s: %s", name, str(raw_args)[:200])
                    invalid_calls.append(
                        ToolCallErrorRecord(
                            name=name,
                            raw_args_preview=str(raw_args)[:500],
                            error_type="invalid_json",
                        )
                    )
                    working_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": '{"error": "Invalid JSON in tool arguments"}',
                        }
                    )
                    continue

                if name not in handlers:
                    self.logger.warning("Ignoring unknown tool call name=%s args=%s", name, args)
                    working_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "content": '{"error": "Tool not available in this runtime"}',
                        }
                    )
                    continue

                self.logger.debug("Executing tool name=%s args=%s", name, args)
                result = handlers[name](**args)
                if inspect.isawaitable(result):
                    result = await result

                records.append(ToolCallRecord(name=name, args=args, result=result))
                self.logger.debug("Tool result name=%s summary=%s", name, self._summarize_tool_result(result))
                working_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": self._stringify_tool_result(result),
                    }
                )

            # Most crawler tools are "fire-and-forget" (persisting results). Avoid a second
            # LLM round to reduce latency and to prevent provider-specific requirements
            # (e.g. DeepSeek thinking mode requiring reasoning_content passback).
            return LLMResult(
                content=last_content,
                tool_call_log=records,
                invalid_tool_calls=invalid_calls,
            )

        self.logger.warning("LLM tool loop stopped after max_rounds=%s", self.max_rounds)
        return LLMResult(
            content=last_content,
            tool_call_log=records,
            invalid_tool_calls=invalid_calls,
        )

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

    def _preview_text(self, content: str, *, limit: int = 300) -> str:
        compact = " ".join(content.strip().split())
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    def _summarize_tool_result(self, result: Any) -> str:
        if isinstance(result, dict):
            links = result.get("links")
            if isinstance(links, list):
                sample = [str(item) for item in links[:3]]
                return f"links={len(links)} sample={sample}"
            saved = result.get("saved")
            if isinstance(saved, int):
                academicians_saved = result.get("academicians_saved")
                if isinstance(academicians_saved, int) and academicians_saved > 0:
                    return f"saved={saved} academicians_saved={academicians_saved}"
                return f"saved={saved}"
            return f"keys={sorted(result.keys())}"
        if isinstance(result, list):
            return f"list[{len(result)}]"
        if isinstance(result, str):
            return f"text[{len(result)}]"
        return type(result).__name__

    def _get(self, value: Any, key: str) -> Any:
        if isinstance(value, dict):
            return value.get(key)
        return getattr(value, key, None)

    def _parse_tool_arguments(self, raw_args: Any) -> dict[str, Any] | None:
        if isinstance(raw_args, dict):
            return raw_args
        if raw_args is None:
            return {}
        if not isinstance(raw_args, str):
            try:
                return dict(raw_args)
            except Exception:
                return None

        text = raw_args.strip()
        if not text:
            return {}

        # Some providers wrap JSON in markdown fences.
        if text.startswith("```"):
            fenced = self._strip_json_fence(text)
            if fenced:
                text = fenced

        candidates = [text]

        escaped = self._escape_unescaped_string_controls(text)
        if escaped != text:
            candidates.append(escaped)

        for candidate in list(candidates):
            stripped_commas = re.sub(r",(\s*[}\]])", r"\1", candidate)
            if stripped_commas != candidate:
                candidates.append(stripped_commas)

            sliced = self._slice_to_json_object(candidate)
            if sliced and sliced != candidate:
                candidates.append(sliced)

            repaired = self._repair_truncated_json_object(candidate)
            if repaired and repaired != candidate:
                candidates.append(repaired)

        seen: set[str] = set()
        for candidate in candidates:
            normalized = candidate.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            try:
                parsed = json.loads(normalized)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        return None

    def _strip_json_fence(self, text: str) -> str:
        lines = text.splitlines()
        if len(lines) < 2:
            return text
        if not lines[0].lstrip().startswith("```"):
            return text
        if not lines[-1].strip().startswith("```"):
            return text
        return "\n".join(lines[1:-1]).strip()

    def _slice_to_json_object(self, text: str) -> str:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return text
        return text[start : end + 1]

    def _escape_unescaped_string_controls(self, text: str) -> str:
        out: list[str] = []
        in_string = False
        escaped = False
        for ch in text:
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == '"':
                out.append(ch)
                in_string = not in_string
                continue
            if in_string and ch in {"\n", "\r", "\t"}:
                if ch == "\n":
                    out.append("\\n")
                elif ch == "\r":
                    out.append("\\r")
                else:
                    out.append("\\t")
                continue
            out.append(ch)
        return "".join(out)

    def _repair_truncated_json_object(self, text: str) -> str | None:
        start = text.find("{")
        if start == -1:
            return None
        candidate = text[start:].strip()
        if not candidate:
            return None

        out: list[str] = []
        stack: list[str] = []
        in_string = False
        escaped = False
        closer_map = {"{": "}", "[": "]"}
        opener_for = {"}": "{", "]": "["}

        for ch in candidate:
            if escaped:
                out.append(ch)
                escaped = False
                continue

            if ch == "\\":
                out.append(ch)
                escaped = True
                continue

            if ch == '"':
                out.append(ch)
                in_string = not in_string
                continue

            if in_string:
                if ch in {"\n", "\r", "\t"}:
                    if ch == "\n":
                        out.append("\\n")
                    elif ch == "\r":
                        out.append("\\r")
                    else:
                        out.append("\\t")
                else:
                    out.append(ch)
                continue

            if ch in closer_map:
                stack.append(ch)
                out.append(ch)
                continue
            if ch in opener_for:
                if stack and stack[-1] == opener_for[ch]:
                    stack.pop()
                    out.append(ch)
                # Drop unmatched closers in malformed outputs.
                continue

            out.append(ch)

        if escaped:
            out.append("\\")
        if in_string:
            out.append('"')

        while stack:
            opener = stack.pop()
            out.append(closer_map[opener])

        return "".join(out).strip()
