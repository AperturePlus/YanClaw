from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler.db_university import _dedupe_org_units_by_name
from agents.crawler.db_utils import _normalize_nullish_columns, _sqlite_has_column


async def ensure_runtime_schema(session: AsyncSession) -> None:
    """Best-effort lightweight schema patching for existing SQLite university DBs."""
    if session.bind is None or session.bind.dialect.name != "sqlite":
        return

    if not await _sqlite_has_column(session, "professors", "org_unit_name"):
        await session.execute(
            text("ALTER TABLE professors ADD COLUMN org_unit_name VARCHAR(255) DEFAULT 'Unknown'")
        )
    if not await _sqlite_has_column(session, "professors", "external_link"):
        await session.execute(text("ALTER TABLE professors ADD COLUMN external_link TEXT"))
    if not await _sqlite_has_column(session, "academicians", "external_link"):
        await session.execute(text("ALTER TABLE academicians ADD COLUMN external_link TEXT"))

    await session.execute(
        text(
            "UPDATE professors SET org_unit_name = 'Unknown' "
            "WHERE org_unit_name IS NULL OR trim(org_unit_name) = ''"
        )
    )
    await session.execute(
        text(
            "UPDATE org_units SET name = trim(name) "
            "WHERE name IS NOT NULL"
        )
    )
    await session.execute(
        text(
            "UPDATE org_units SET name = 'Unknown' "
            "WHERE name IS NULL OR trim(name) = ''"
        )
    )
    await _dedupe_org_units_by_name(session)
    await session.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_org_units_name ON org_units(name)"))

    await _normalize_nullish_columns(
        session,
        "professors",
        [
            "title",
            "research_areas",
            "email",
            "phone",
            "homepage",
            "external_link",
            "bio",
            "enrollment_pref",
            "publications",
        ],
    )
    await _normalize_nullish_columns(
        session,
        "academicians",
        [
            "title",
            "research_areas",
            "email",
            "phone",
            "homepage",
            "external_link",
            "bio",
            "enrollment_pref",
            "publications",
        ],
    )


__all__ = ["ensure_runtime_schema"]

