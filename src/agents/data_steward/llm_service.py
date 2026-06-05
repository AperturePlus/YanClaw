from __future__ import annotations

import json
from typing import Any

from agents.crawler.config import CrawlerSettings
from runtime.context import ContextManager
from runtime.database import DatabaseManager
from runtime.llm import LLMClient
from runtime.logger import get_logger
from runtime.skills import SkillManager


STEWARD_MISSING_FIELD_TRIAGE = "STEWARD_MISSING_FIELD_TRIAGE"
STEWARD_PROFILE_CLEANUP = "STEWARD_PROFILE_CLEANUP"
STEWARD_IDENTITY_REVIEW = "STEWARD_IDENTITY_REVIEW"
STEWARD_ORG_UNIT_CLEANUP = "STEWARD_ORG_UNIT_CLEANUP"


class DataStewardLLMService:
    def __init__(self, *, settings: CrawlerSettings, db: DatabaseManager) -> None:
        self.settings = settings
        self.context_manager = ContextManager(settings.openai_model)
        self.skill_manager = SkillManager(settings.data_steward_skills_dir, db, "data_steward")
        self.logger = get_logger("steward.llm")
        self.llm_client = LLMClient(
            settings.openai_base_url,
            settings.openai_api_key,
            settings.openai_model,
            timeout_seconds=settings.llm_timeout_seconds,
            temperature=settings.llm_temperature,
            top_p=settings.llm_top_p,
            seed=settings.llm_seed,
            max_rounds=1,
        )

    def skills_text(self, state: str) -> str:
        return self.skill_manager.select_for_state(state, set()).rendered_text

    async def classify_missing_fields(
        self,
        rows: list[dict[str, Any]],
        max_context_tokens: int,
    ) -> dict[int, dict[str, Any]]:
        parsed = await self._run_items_json(
            state=STEWARD_MISSING_FIELD_TRIAGE,
            rows=rows,
            max_context_tokens=max_context_tokens,
            system_prompt=(
                "You are a DataSteward missing-field triage reviewer. "
                "Return strict JSON with an items array."
            ),
        )
        result: dict[int, dict[str, Any]] = {}
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                row_id = int(item.get("id") or item.get("professor_id"))
            except Exception:
                continue
            result[row_id] = item
        return result

    async def cleanup_profiles(
        self,
        rows: list[dict[str, Any]],
        max_context_tokens: int,
    ) -> dict[str, dict[str, Any]]:
        parsed = await self._run_items_json(
            state=STEWARD_PROFILE_CLEANUP,
            rows=rows,
            max_context_tokens=max_context_tokens,
            system_prompt=(
                "You are a DataSteward profile cleanup reviewer. "
                "Return strict JSON with an items array."
            ),
        )
        return _items_by_entity_key(parsed)

    async def review_identities(
        self,
        rows: list[dict[str, Any]],
        max_context_tokens: int,
    ) -> dict[str, dict[str, Any]]:
        parsed = await self._run_items_json(
            state=STEWARD_IDENTITY_REVIEW,
            rows=rows,
            max_context_tokens=max_context_tokens,
            system_prompt=(
                "You are a DataSteward academician identity reviewer. "
                "Return strict JSON with an items array."
            ),
        )
        return _items_by_entity_key(parsed)

    async def _run_items_json(
        self,
        *,
        state: str,
        rows: list[dict[str, Any]],
        max_context_tokens: int,
        system_prompt: str,
    ) -> list[dict[str, Any]]:
        if not rows or not (self.settings.openai_api_key or "").strip():
            return []
        budget = max(4096, min(int(max_context_tokens), 256000))
        output: list[dict[str, Any]] = []
        batches = self._chunk_rows_for_llm(rows, budget)
        self.logger.info(
            "DataSteward LLM state=%s rows=%s chunks=%s",
            state,
            len(rows),
            len(batches),
        )
        for index, batch in enumerate(batches, start=1):
            self.logger.info(
                "DataSteward LLM state=%s chunk=%s/%s rows=%s start",
                state,
                index,
                len(batches),
                len(batch),
            )
            payload = json.dumps({"state": state, "items": batch}, ensure_ascii=False, sort_keys=True)
            messages = self.context_manager.build_messages(
                system_prompt,
                [],
                self.skills_text(state),
                payload,
                budget,
                dynamic_system_content="Tool call policy: Do not call any tools.",
            )[0]
            try:
                response = await self.llm_client.chat(messages, tools=None, tool_handlers={})
            except Exception as error:
                self.logger.warning("DataSteward LLM call failed state=%s error=%s", state, error)
                return output
            parsed = parse_json_object(response.content)
            items = parsed.get("items") if isinstance(parsed, dict) else None
            if isinstance(items, list):
                valid_items = [item for item in items if isinstance(item, dict)]
                output.extend(valid_items)
                self.logger.info(
                    "DataSteward LLM state=%s chunk=%s/%s returned_items=%s",
                    state,
                    index,
                    len(batches),
                    len(valid_items),
                )
            else:
                self.logger.info(
                    "DataSteward LLM state=%s chunk=%s/%s returned_items=0",
                    state,
                    index,
                    len(batches),
                )
        return output

    def _chunk_rows_for_llm(self, rows: list[dict[str, Any]], budget: int) -> list[list[dict[str, Any]]]:
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for row in rows:
            candidate = current + [row]
            content = json.dumps({"items": candidate}, ensure_ascii=False)
            if current and self.context_manager.count_tokens(content) > budget:
                chunks.append(current)
                current = [row]
                continue
            current = candidate
        if current:
            chunks.append(current)
        return chunks


def parse_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _items_by_entity_key(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item.get("entity_key") or item.get("id") or "").strip()
        if key:
            result[key] = item
    return result
