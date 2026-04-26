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
