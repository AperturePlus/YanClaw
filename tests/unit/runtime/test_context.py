from __future__ import annotations

from runtime.context import ContextManager


def test_context_manager_counts_chunks_and_truncates():
    context = ContextManager()
    assert context.count_tokens("hello world") > 0

    text = "\n\n".join(f"paragraph {index} " * 20 for index in range(10))
    chunks = context.chunk_text(text, max_tokens=30)
    assert len(chunks) > 1
    assert all(context.count_tokens(chunk) <= 30 for chunk in chunks)

    batches = context.build_messages("system", [], "", text, max_tokens=60)
    assert len(batches) > 1
    assert all(batch[0]["role"] == "system" for batch in batches)

    truncated = context.truncate_list(["a " * 20, "b", "c"], max_tokens=3)
    assert truncated == ["b", "c"]


def test_context_compact_text_is_deterministic():
    context = ContextManager()
    text = (
        "新闻 通知\n"
        "Professor Ada, email: ada@example.edu\n"
        "新闻 通知\n"
        "Professor Ada, email: ada@example.edu\n"
        "电话: 123456\n"
    )
    first = context.compact_text(text)
    second = context.compact_text(text)
    assert first == second
    assert first.count("Professor Ada") == 1
    assert "ada@example.edu" in first


def test_context_build_messages_keeps_stable_system_across_chunks():
    context = ContextManager()
    user_text = "\n\n".join(f"paragraph {i} " * 30 for i in range(8))
    tool_defs = [
        {"type": "function", "name": "z_tool", "description": "z"},
        {"type": "function", "name": "a_tool", "description": "a"},
    ]
    batches = context.build_messages(
        "system",
        tool_defs,
        "skills",
        user_text,
        max_tokens=80,
        dynamic_system_content="Tool call policy: test",
    )
    assert len(batches) > 1
    stable_system = batches[0][0]["content"]
    assert all(batch[0]["content"] == stable_system for batch in batches)
    assert "a_tool" in stable_system and "z_tool" in stable_system
    assert stable_system.index("a_tool") < stable_system.index("z_tool")
    assert all(batch[1]["role"] == "system" for batch in batches)
    assert all(batch[2]["role"] == "user" for batch in batches)
