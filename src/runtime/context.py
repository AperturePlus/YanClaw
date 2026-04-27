from __future__ import annotations

import json
from typing import Any

import tiktoken


class ContextManager:
    """Token budget helper for building LLM message batches."""

    def __init__(self, model: str = "gpt-4o-mini") -> None:
        self.model = model
        try:
            self.encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            self.encoding = tiktoken.get_encoding("cl100k_base")

    def count_tokens(self, text: str) -> int:
        if not text:
            return 0
        return len(self.encoding.encode(text))

    def build_messages(
        self,
        system_prompt: str,
        tool_defs: Any,
        skills_text: str,
        user_content: str,
        max_tokens: int,
    ) -> list[list[dict[str, str]]]:
        # ~4 tokens per message for role/formatting overhead, plus 2 for priming
        message_overhead = 4 * 2 + 2  # 2 messages (system + user) * 4 tokens each + 2 priming
        tools_text = tool_defs if isinstance(tool_defs, str) else self._summarize_tools(tool_defs)
        fixed_parts = [
            system_prompt,
            f"Available tools:\n{tools_text}" if tools_text else "",
            f"Loaded skills:\n{skills_text}" if skills_text else "",
        ]
        fixed_text = "\n\n".join(part for part in fixed_parts if part)
        fixed_tokens = self.count_tokens(fixed_text) + message_overhead
        content_budget = max(1, max_tokens - fixed_tokens)

        chunks = [user_content]
        if self.count_tokens(user_content) > content_budget:
            chunks = self.chunk_text(user_content, content_budget)

        batches: list[list[dict[str, str]]] = []
        for index, chunk in enumerate(chunks, start=1):
            suffix = f"\n\nChunk {index}/{len(chunks)}." if len(chunks) > 1 else ""
            batches.append(
                [
                    {"role": "system", "content": fixed_text + suffix},
                    {"role": "user", "content": chunk},
                ]
            )
        return batches

    def _summarize_tools(self, tool_defs: Any) -> str:
        if not tool_defs:
            return ""
        if isinstance(tool_defs, str):
            return tool_defs
        if not isinstance(tool_defs, list):
            return json.dumps(tool_defs, ensure_ascii=False)

        lines: list[str] = []
        for tool in tool_defs:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") != "function":
                continue
            function = tool.get("function") if isinstance(tool.get("function"), dict) else None
            name = (function.get("name") if function else tool.get("name")) or ""
            description = (function.get("description") if function else tool.get("description")) or ""
            if not name:
                continue
            line = f"- {name}: {description}".rstrip(": ").strip()
            lines.append(line)

        if lines:
            return "\n".join(lines)
        return json.dumps(tool_defs, ensure_ascii=False)

    def chunk_text(self, text: str, max_tokens: int) -> list[str]:
        if self.count_tokens(text) <= max_tokens:
            return [text]

        paragraphs = text.split("\n\n")
        chunks: list[str] = []
        current: list[str] = []
        current_tokens = 0

        for paragraph in paragraphs:
            paragraph_tokens = self.count_tokens(paragraph)
            if paragraph_tokens > max_tokens:
                if current:
                    chunks.append("\n\n".join(current))
                    current = []
                    current_tokens = 0
                chunks.extend(self._chunk_by_tokens(paragraph, max_tokens))
                continue

            separator_tokens = self.count_tokens("\n\n") if current else 0
            if current and current_tokens + separator_tokens + paragraph_tokens > max_tokens:
                chunks.append("\n\n".join(current))
                current = [paragraph]
                current_tokens = paragraph_tokens
            else:
                current.append(paragraph)
                current_tokens += separator_tokens + paragraph_tokens

        if current:
            chunks.append("\n\n".join(current))
        return chunks

    def truncate_list(self, items: list[str], max_tokens: int) -> list[str]:
        kept: list[str] = []
        total = 0
        for item in reversed(items):
            item_tokens = self.count_tokens(item)
            if kept and total + item_tokens > max_tokens:
                break
            if item_tokens <= max_tokens:
                kept.insert(0, item)
                total += item_tokens
        return kept

    def _chunk_by_tokens(self, text: str, max_tokens: int) -> list[str]:
        tokens = self.encoding.encode(text)
        chunks = []
        for start in range(0, len(tokens), max_tokens):
            chunks.append(self.encoding.decode(tokens[start : start + max_tokens]))
        return chunks
