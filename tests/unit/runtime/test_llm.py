from __future__ import annotations

import pytest

from runtime.llm import LLMClient, LLMResponseError


class _Completions:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class _Chat:
    def __init__(self, responses):
        self.completions = _Completions(responses)


class _Client:
    def __init__(self, responses):
        self.chat = _Chat(responses)


def _response(message):
    return {"choices": [{"message": message}]}


async def test_llm_client_plain_chat():
    client = LLMClient("http://example", "key", "model", client=_Client([_response({"content": "ok"})]))
    result = await client.chat([{"role": "user", "content": "hello"}])
    assert result.content == "ok"
    assert result.tool_call_log == []


async def test_llm_client_tool_call_loop():
    responses = [
        _response(
            {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "add", "arguments": '{"x": 2, "y": 3}'},
                    }
                ],
            }
        ),
        _response({"content": "done"}),
    ]
    client = LLMClient("http://example", "key", "model", client=_Client(responses))

    async def add(x, y):
        return {"value": x + y}

    result = await client.chat(
        [{"role": "user", "content": "add"}],
        tools=[{"type": "function", "function": {"name": "add"}}],
        tool_handlers={"add": add},
    )
    assert result.content == "done"
    assert result.tool_call_log[0].result == {"value": 5}


async def test_llm_client_stops_at_max_rounds():
    responses = [
        _response(
            {
                "content": "again",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "noop", "arguments": "{}"},
                    }
                ],
            }
        )
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
        tools=[{"type": "function", "function": {"name": "noop"}}],
        tool_handlers={"noop": lambda: "ok"},
    )
    assert result.content == "again"
    assert len(result.tool_call_log) == 1


async def test_llm_client_reports_invalid_chat_response():
    client = LLMClient("http://example", "key", "model", client=_Client([{"error": "bad"}]))

    with pytest.raises(LLMResponseError, match="did not include chat completion choices"):
        await client.chat([{"role": "user", "content": "hello"}])


async def test_llm_client_passes_max_tokens_to_chat_completion():
    fake_client = _Client([_response({"content": "ok"})])
    client = LLMClient("http://example", "key", "model", client=fake_client)

    await client.chat([{"role": "user", "content": "hello"}], max_tokens=1)

    assert fake_client.chat.completions.calls[0]["max_tokens"] == 1
