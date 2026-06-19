from __future__ import annotations

from enum import Enum


class CrawlerState(str, Enum):
    DISCOVER_ORG_UNIT_PAGES = "DISCOVER_ORG_UNIT_PAGES"
    EXTRACT_ORG_UNITS = "EXTRACT_ORG_UNITS"
    FIND_FACULTY_PAGES = "FIND_FACULTY_PAGES"
    EXTRACT_PROFESSORS = "EXTRACT_PROFESSORS"
    DONE = "DONE"


# Detail children inherit the parent faculty page's priority plus this boost so a
# college's subtree is claimed contiguously (per-college locality, design §4.2).
# Lives here (a dependency-free module) to avoid the agent_detail <->
# extraction_pipeline import cycle.
_DETAIL_PRIORITY_INHERIT_BOOST = 20.0
