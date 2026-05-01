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


async def test_llm_client_repairs_unescaped_newline_in_tool_arguments():
    raw_args = (
        '{"org_unit_name":"School of Economics","org_unit_url":"https://sesu.scu.edu.cn/",'
        '"source_url":"https://sesu.scu.edu.cn/info/1128/9772.htm",'
        '"professors":[{"name":"Zeng Zhongdong","title":"Professor","research_areas":"Risk Management\nMacro Economics"}]}'
    )
    responses = [
        _chat_response(
            content="need tool",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "save_professors",
                        "arguments": raw_args,
                    },
                }
            ],
        )
    ]
    client = LLMClient("http://example", "key", "model", client=_Client(responses))
    seen: dict = {}

    async def save_professors(**kwargs):
        seen.update(kwargs)
        return {"saved": 1}

    result = await client.chat(
        [{"role": "user", "content": "test"}],
        tools=[{"type": "function", "name": "save_professors"}],
        tool_handlers={"save_professors": save_professors},
    )

    assert result.tool_call_log
    assert seen["org_unit_name"] == "School of Economics"
    assert seen["professors"][0]["name"] == "Zeng Zhongdong"
    assert "Risk Management" in seen["professors"][0]["research_areas"]
