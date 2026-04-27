from __future__ import annotations

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


def _text_response(text: str) -> dict:
    """Build a Responses API output with a text message."""
    return {
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }


def _tool_response(tool_calls: list[dict], text: str = "") -> dict:
    """Build a Responses API output with function_call items."""
    items: list[dict] = []
    if text:
        items.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        })
    for tc in tool_calls:
        items.append({
            "type": "function_call",
            "call_id": tc["call_id"],
            "name": tc["name"],
            "arguments": tc["arguments"],
        })
    return {"output": items}


async def test_llm_client_plain_chat():
    client = LLMClient("http://example", "key", "model", client=_Client([_text_response("ok")]))
    result = await client.chat([{"role": "user", "content": "hello"}])
    assert result.content == "ok"
    assert result.tool_call_log == []


async def test_llm_client_tool_call_loop():
    responses = [
        _tool_response([{"call_id": "call_1", "name": "add", "arguments": '{"x": 2, "y": 3}'}]),
        _text_response("done"),
    ]
    client = LLMClient("http://example", "key", "model", client=_Client(responses))

    async def add(x, y):
        return {"value": x + y}

    result = await client.chat(
        [{"role": "user", "content": "add"}],
        tools=[{"type": "function", "name": "add"}],
        tool_handlers={"add": add},
    )
    assert result.content == "done"
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
    client = LLMClient("http://example", "key", "model", client=_Client([{"output": []}]))
    result = await client.chat([{"role": "user", "content": "hello"}])
    # Empty output → empty content, no error (Responses API returns empty output for no content)
    assert result.content == ""


async def test_llm_client_passes_max_tokens_to_responses_api():
    fake_client = _Client([_text_response("ok")])
    client = LLMClient("http://example", "key", "model", client=fake_client)

    await client.chat([{"role": "user", "content": "hello"}], max_tokens=1)

    assert fake_client.responses.calls[0]["max_output_tokens"] == 1
