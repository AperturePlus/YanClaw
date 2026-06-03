from __future__ import annotations

from typing import Any


class FacultyDiscoveryService:
    """Faculty-page discovery boundary around the existing heuristic flow."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    async def find_and_extract_streaming(self, org_units: list[Any]) -> None:
        await self.agent._find_and_extract_streaming_impl(org_units)

    async def find_faculty_pages(self, org_units: list[Any]) -> list[Any]:
        return await self.agent._find_faculty_pages_impl(org_units)
