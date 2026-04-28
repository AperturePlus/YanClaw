from __future__ import annotations

from runtime.llm import LLMClient


class _Completions:
    def __init__(self, responses):
        self._responses = list(responses)

    async def create(self, **kwargs):
        return self._responses.pop(0)


class _Chat:
    def __init__(self, responses):
        self.completions = _Completions(responses)


class _Client:
    def __init__(self, responses):
        self.chat = _Chat(responses)


def _chat_response(*, content: str, tool_calls: list[dict] | None = None) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content,
                    "tool_calls": tool_calls or [],
                }
            }
        ]
    }


async def test_llm_client_ignores_unknown_tool_calls():
    responses = [
        _chat_response(
            content="need tool",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "navigate",
                        "arguments": '{"url":"https://example.edu.cn"}',
                    },
                }
            ],
        )
    ]
    client = LLMClient("http://example", "key", "model", client=_Client(responses))

    result = await client.chat(
        [{"role": "user", "content": "test"}],
        tools=[{"type": "function", "name": "save_professors"}],
        tool_handlers={"save_professors": lambda **kwargs: {"saved": 0}},
    )

    assert result.content == "need tool"
    assert result.tool_call_log == []
