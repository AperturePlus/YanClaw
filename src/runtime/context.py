from __future__ import annotations

import json
import re
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
        dynamic_system_content: str = "",
    ) -> list[list[dict[str, str]]]:
        # ~4 tokens per message for role/formatting overhead, plus 2 for priming.
        tools_text = tool_defs if isinstance(tool_defs, str) else self._summarize_tools(tool_defs)
        stable_parts = [
            system_prompt,
            f"Available tools:\n{tools_text}" if tools_text else "",
            f"Loaded skills:\n{skills_text}" if skills_text else "",
        ]
        stable_text = "\n\n".join(part for part in stable_parts if part)
        dynamic_text = (dynamic_system_content or "").strip()
        message_count = 2 + (1 if dynamic_text else 0)  # system + user (+ optional dynamic system)
        message_overhead = 4 * message_count + 2
        fixed_tokens = self.count_tokens(stable_text) + self.count_tokens(dynamic_text) + message_overhead
        content_budget = max(1, max_tokens - fixed_tokens)

        chunks = [user_content]
        if self.count_tokens(user_content) > content_budget:
            chunks = self.chunk_text(user_content, content_budget)

        batches: list[list[dict[str, str]]] = []
        for index, chunk in enumerate(chunks, start=1):
            user_chunk = chunk
            if len(chunks) > 1:
                user_chunk = f"[chunk {index}/{len(chunks)}]\n{chunk}"

            batch: list[dict[str, str]] = [{"role": "system", "content": stable_text}]
            if dynamic_text:
                batch.append({"role": "system", "content": dynamic_text})
            batch.append({"role": "user", "content": user_chunk})
            batches.append(batch)
        return batches

    def _summarize_tools(self, tool_defs: Any) -> str:
        if not tool_defs:
            return ""
        if isinstance(tool_defs, str):
            return tool_defs
        if not isinstance(tool_defs, list):
            return json.dumps(tool_defs, ensure_ascii=False)

        line_items: list[tuple[str, str]] = []
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
            line_items.append((name, line))

        lines = [item[1] for item in sorted(line_items, key=lambda item: item[0])]

        if lines:
            return "\n".join(lines)
        return json.dumps(tool_defs, ensure_ascii=False)

    def compact_text(self, text: str) -> str:
        if not text:
            return ""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        normalized = re.sub(r"[ \t]+", " ", normalized)

        lines: list[str] = []
        seen: set[str] = set()
        for raw in normalized.split("\n"):
            line = " ".join(raw.split()).strip()
            if not line:
                continue
            key = line.lower()
            if key in seen:
                continue
            seen.add(key)
            lines.append(line)

        if not lines:
            return ""

        signal_tokens = (
            "faculty",
            "teacher",
            "staff",
            "professor",
            "email",
            "phone",
            "research",
            "homepage",
            "导师",
            "教师",
            "师资",
            "教授",
            "副教授",
            "讲师",
            "研究员",
            "邮箱",
            "电话",
            "研究方向",
            "博导",
            "硕导",
        )
        noise_tokens = (
            "新闻",
            "通知",
            "公告",
            "政策",
            "党建",
            "学生工作",
            "招生",
            "就业",
            "招聘",
            "人事",
            "copyright",
            "all rights reserved",
            "版权所有",
            "点击量",
            "上一篇",
            "下一篇",
            "地址：",
            "邮编：",
            "icp",
            "公安备案",
            "news",
            "notice",
            "announcement",
            "policy",
            "recruit",
            "hr",
            "personnel",
        )

        keep_indices: set[int] = set()
        for idx, line in enumerate(lines):
            lowered = line.lower()
            has_signal = ("@" in line) or any(token in lowered for token in signal_tokens)
            has_noise = any(token in lowered for token in noise_tokens)
            if has_signal:
                for offset in (-1, 0, 1):
                    neighbor = idx + offset
                    if 0 <= neighbor < len(lines):
                        keep_indices.add(neighbor)
                continue
            if has_noise and len(line) <= 160:
                continue
            keep_indices.add(idx)

        compacted_lines: list[str] = []
        for idx, line in enumerate(lines):
            if idx not in keep_indices:
                continue
            if len(line) > 500:
                line = line[:500].rstrip()
            compacted_lines.append(line)
        return "\n".join(compacted_lines).strip()

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
