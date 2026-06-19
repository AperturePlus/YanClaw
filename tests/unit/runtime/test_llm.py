from __future__ import annotations

import asyncio

import pytest

from runtime.llm import LLMClient, LLMResponseError


class _Responses:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _Client:
    def __init__(self, responses):
        self.responses = _Responses(responses)
        self.chat = type("Chat", (), {"completions": self.responses})()


def _text_response(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _tool_response(tool_calls: list[dict], text: str = "") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": text,
                    "tool_calls": [
                        {
                            "id": tc["call_id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": tc["arguments"],
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            }
        ]
    }


async def test_llm_client_plain_chat():
    client = LLMClient("http://example", "key", "model", client=_Client([_text_response("ok")]))
    result = await client.chat([{"role": "user", "content": "hello"}])
    assert result.content == "ok"
    assert result.tool_call_log == []


async def test_llm_client_tool_call_loop():
    responses = [
        _tool_response([{"call_id": "call_1", "name": "add", "arguments": '{"x": 2, "y": 3}'}]),
    ]
    client = LLMClient("http://example", "key", "model", client=_Client(responses))

    async def add(x, y):
        return {"value": x + y}

    result = await client.chat(
        [{"role": "user", "content": "add"}],
        tools=[{"type": "function", "name": "add"}],
        tool_handlers={"add": add},
    )
    assert result.content == ""
    assert result.tool_call_log[0].result == {"value": 5}


async def test_llm_client_stops_at_max_rounds():
    responses = [
        _tool_response([{"call_id": "call_1", "name": "noop", "arguments": "{}"}], text="again"),
    ]
    client = LLMClient(
        "http://example",
        "key",
        "model",
        max_rounds=1,
        client=_Client(responses),
    )
    result = await client.chat(
        [{"role": "user", "content": "loop"}],
        tools=[{"type": "function", "name": "noop"}],
        tool_handlers={"noop": lambda: "ok"},
    )
    assert result.content == "again"
    assert len(result.tool_call_log) == 1


async def test_llm_client_reports_invalid_chat_response():
    client = LLMClient("http://example", "key", "model", client=_Client([{"choices": []}]))

    with pytest.raises(LLMResponseError):
        await client.chat([{"role": "user", "content": "hello"}])


async def test_llm_client_passes_max_tokens_to_chat_completions():
    fake_client = _Client([_text_response("ok")])
    client = LLMClient("http://example", "key", "model", client=fake_client)

    await client.chat([{"role": "user", "content": "hello"}], max_tokens=1)

    assert fake_client.responses.calls[0]["max_tokens"] == 1


async def test_llm_client_allows_parallel_calls_without_default_rate_limit():
    class _ConcurrentResponses:
        def __init__(self, *, release_after: int):
            self.calls = []
            self.active = 0
            self.max_active = 0
            self.release_after = release_after
            self.all_started = asyncio.Event()

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if len(self.calls) >= self.release_after:
                self.all_started.set()
            await self.all_started.wait()
            self.active -= 1
            return _text_response("ok")

    responses = _ConcurrentResponses(release_after=4)
    fake_client = type("Client", (), {"chat": type("Chat", (), {"completions": responses})()})()
    client = LLMClient(
        "http://example",
        "key",
        "model",
        max_concurrent=4,
        min_interval=0,
        client=fake_client,
    )

    await asyncio.wait_for(
        asyncio.gather(
            *(client.chat([{"role": "user", "content": f"hello {index}"}]) for index in range(4))
        ),
        timeout=1,
    )

    assert responses.max_active == 4
