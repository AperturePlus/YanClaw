from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agents.crawler.models import Academician, Professor, ProfessorAffiliation
from agents.crawler.sanitizer import normalize_name, normalize_name_key
from agents.crawler.db.university import _dedupe_org_units_by_name
from agents.crawler.db.utils import (
    _merge_org_unit_names,
    _normalize_nullish_columns,
    _now_utc,
    _sqlite_has_column,
)


async def ensure_runtime_schema(session: AsyncSession) -> None:
    """Best-effort lightweight schema patching for existing SQLite university DBs."""
    if session.bind is None or session.bind.dialect.name != "sqlite":
        return

    await session.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS crawl_page_cache (
                id INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                final_url TEXT NOT NULL DEFAULT '',
                status_code INTEGER NOT NULL DEFAULT 0,
                text_snapshot TEXT NOT NULL DEFAULT '',
                links_json TEXT NOT NULL DEFAULT '[]',
                link_signals_json TEXT NOT NULL DEFAULT '[]',
                block_reason TEXT,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
    )
    await session.execute(
        text("CREATE UNIQUE INDEX IF NOT EXISTS uq_crawl_page_cache_url ON crawl_page_cache(url)")
    )
    await session.execute(
        text("CREATE INDEX IF NOT EXISTS ix_crawl_page_cache_final_url ON crawl_page_cache(final_url)")
    )

    if not await _sqlite_has_column(session, "professors", "org_unit_name"):
        await session.execute(
            text("ALTER TABLE professors ADD COLUMN org_unit_name VARCHAR(255) DEFAULT 'Unknown'")
        )
    if not await _sqlite_has_column(session, "professors", "name_key"):
        await session.execute(text("ALTER TABLE professors ADD COLUMN name_key VARCHAR(255) DEFAULT ''"))
    if not await _sqlite_has_column(session, "professors", "external_link"):
        await session.execute(text("ALTER TABLE professors ADD COLUMN external_link TEXT"))
    if not await _sqlite_has_column(session, "academicians", "name_key"):
        await session.execute(text("ALTER TABLE academicians ADD COLUMN name_key VARCHAR(255) DEFAULT ''"))
    if not await _sqlite_has_column(session, "academicians", "external_link"):
        await session.execute(text("ALTER TABLE academicians ADD COLUMN external_link TEXT"))
    if not await _sqlite_has_column(session, "crawl_tasks", "task_kind"):
        await session.execute(
            text("ALTER TABLE crawl_tasks ADD COLUMN task_kind VARCHAR(32) DEFAULT 'list_page'")
        )
    await session.execute(
        text(
            "UPDATE crawl_tasks SET task_kind = 'list_page' "
            "WHERE task_kind IS NULL OR trim(task_kind) = ''"
        )
    )

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
    await _backfill_name_keys(session)
    await _dedupe_professors_by_org_unit_name_key(session)
    await _dedupe_academicians_by_org_unit_name_key(session)
    await _normalize_entity_names(session)
    await session.execute(text("CREATE INDEX IF NOT EXISTS ix_professors_name_key ON professors(name_key)"))
    await session.execute(text("CREATE INDEX IF NOT EXISTS ix_academicians_name_key ON academicians(name_key)"))
    await session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_academicians_org_unit_name_key "
            "ON academicians(org_unit_id, name_key) "
            "WHERE name_key IS NOT NULL AND trim(name_key) <> ''"
        )
    )


async def _backfill_name_keys(session: AsyncSession) -> None:
    for row in (await session.execute(select(Professor))).scalars().all():
        key = normalize_name_key(row.name)
        if getattr(row, "name_key", None) != key:
            row.name_key = key
    for row in (await session.execute(select(Academician))).scalars().all():
        key = normalize_name_key(row.name)
        if getattr(row, "name_key", None) != key:
            row.name_key = key
    await session.flush()


async def _normalize_entity_names(session: AsyncSession) -> None:
    for row in (await session.execute(select(Professor))).scalars().all():
        clean_name = normalize_name(row.name)
        if clean_name and row.name != clean_name:
            row.name = clean_name
        key = normalize_name_key(clean_name or row.name)
        if row.name_key != key:
            row.name_key = key
    for row in (await session.execute(select(Academician))).scalars().all():
        clean_name = normalize_name(row.name)
        if clean_name and row.name != clean_name:
            row.name = clean_name
        key = normalize_name_key(clean_name or row.name)
        if row.name_key != key:
            row.name_key = key
    await session.flush()


async def _dedupe_professors_by_org_unit_name_key(session: AsyncSession) -> None:
    groups = (
        await session.execute(
            select(ProfessorAffiliation.org_unit_id, Professor.name_key)
            .join(Professor, Professor.id == ProfessorAffiliation.professor_id)
            .where(Professor.name_key.is_not(None), func.trim(Professor.name_key) != "")
            .group_by(ProfessorAffiliation.org_unit_id, Professor.name_key)
            .having(func.count(func.distinct(Professor.id)) > 1)
        )
    ).all()
    for org_unit_id, name_key in groups:
        rows = (
            await session.execute(
                select(Professor)
                .join(ProfessorAffiliation, ProfessorAffiliation.professor_id == Professor.id)
                .where(
                    ProfessorAffiliation.org_unit_id == int(org_unit_id),
                    Professor.name_key == str(name_key),
                )
                .order_by(Professor.id.asc())
            )
        ).scalars().all()
        unique_rows = list(dict.fromkeys(rows))
        if len(unique_rows) < 2:
            continue
        keeper = max(unique_rows, key=_entity_keeper_score)
        clean_name = normalize_name(keeper.name)
        if clean_name:
            keeper.name = clean_name
            keeper.name_key = normalize_name_key(clean_name)
        for victim in unique_rows:
            if int(victim.id) == int(keeper.id):
                continue
            await _merge_professor_duplicate(session, keeper, victim)
        keeper.updated_at = _now_utc()
    await session.flush()


async def _dedupe_academicians_by_org_unit_name_key(session: AsyncSession) -> None:
    groups = (
        await session.execute(
            select(Academician.org_unit_id, Academician.name_key)
            .where(Academician.name_key.is_not(None), func.trim(Academician.name_key) != "")
            .group_by(Academician.org_unit_id, Academician.name_key)
            .having(func.count(Academician.id) > 1)
        )
    ).all()
    for org_unit_id, name_key in groups:
        rows = (
            await session.execute(
                select(Academician)
                .where(
                    Academician.org_unit_id == int(org_unit_id),
                    Academician.name_key == str(name_key),
                )
                .order_by(Academician.id.asc())
            )
        ).scalars().all()
        if len(rows) < 2:
            continue
        keeper = max(rows, key=_entity_keeper_score)
        clean_name = normalize_name(keeper.name)
        if clean_name:
            keeper.name = clean_name
            keeper.name_key = normalize_name_key(clean_name)
        for victim in rows:
            if int(victim.id) == int(keeper.id):
                continue
            _merge_academician_duplicate(keeper, victim)
            await session.delete(victim)
        keeper.updated_at = _now_utc()
    await session.flush()


async def _merge_professor_duplicate(
    session: AsyncSession,
    keeper: Professor,
    victim: Professor,
) -> None:
    from agents.crawler.db.professors import _choose_better_profile_url

    _merge_common_fields(keeper, victim)
    keeper.org_unit_name = _merge_org_unit_names(keeper.org_unit_name, victim.org_unit_name)
    for field_name in ("homepage", "external_link"):
        best = _choose_better_profile_url(getattr(keeper, field_name), getattr(victim, field_name))
        if best:
            setattr(keeper, field_name, best)

    affiliations = (
        await session.execute(
            select(ProfessorAffiliation).where(ProfessorAffiliation.professor_id == int(victim.id))
        )
    ).scalars().all()
    for affiliation in affiliations:
        existing = (
            await session.execute(
                select(ProfessorAffiliation).where(
                    ProfessorAffiliation.professor_id == int(keeper.id),
                    ProfessorAffiliation.org_unit_id == int(affiliation.org_unit_id),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            affiliation.professor_id = int(keeper.id)
            continue
        better_source = _choose_better_profile_url(existing.source_url, affiliation.source_url)
        if better_source and better_source != existing.source_url:
            existing.source_url = better_source
        await session.delete(affiliation)

    await session.delete(victim)


def _merge_academician_duplicate(keeper: Academician, victim: Academician) -> None:
    from agents.crawler.db.professors import _choose_better_profile_url

    _merge_common_fields(keeper, victim)
    for field_name in ("homepage", "external_link", "source_url"):
        best = _choose_better_profile_url(getattr(keeper, field_name), getattr(victim, field_name))
        if best:
            setattr(keeper, field_name, best)


def _merge_common_fields(keeper: Any, victim: Any) -> None:
    for field_name in (
        "title",
        "research_areas",
        "email",
        "phone",
        "bio",
        "enrollment_pref",
        "publications",
    ):
        current = getattr(keeper, field_name, None)
        incoming = getattr(victim, field_name, None)
        if current in {None, ""} and incoming not in {None, ""}:
            setattr(keeper, field_name, incoming)


def _entity_keeper_score(row: Any) -> tuple[int, int, int]:
    raw_name = str(getattr(row, "name", "") or "").strip()
    clean_name = normalize_name(raw_name)
    name_score = 1 if raw_name == clean_name else 0
    completeness = sum(
        1
        for field_name in (
            "title",
            "research_areas",
            "email",
            "phone",
            "homepage",
            "external_link",
            "bio",
            "enrollment_pref",
            "publications",
            "source_url",
        )
        if getattr(row, field_name, None) not in {None, ""}
    )
    return name_score, completeness, -int(getattr(row, "id", 0) or 0)


__all__ = ["ensure_runtime_schema"]
