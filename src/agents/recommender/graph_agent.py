from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

from agents.crawler.config import CrawlerSettings
from agents.data_steward.db.selector import resolve_targets
from agents.recommender.db import repository
from agents.recommender.text import concept_key, extract_concepts, tokenize
from agents.recommender.types import GraphBuildSummary
from runtime.logger import get_logger


@dataclass(frozen=True)
class _SourceSnapshot:
    meta: dict[str, Any]
    org_units: list[dict[str, Any]]
    professors: list[dict[str, Any]]
    academicians: list[dict[str, Any]]
    affiliations: list[dict[str, Any]]


class KnowledgeGraphAgent:
    """Build a local SQLite knowledge graph from per-university crawler DBs."""

    def __init__(self, *, settings: CrawlerSettings, graph_db_path: Path | None = None) -> None:
        self.settings = settings
        self.graph_db_path = Path(graph_db_path or settings.knowledge_graph_db_path)
        self.logger = get_logger("recommender.graph")

    async def build(
        self,
        *,
        universities: list[str] | None = None,
        db_roots: list[str] | None = None,
        rebuild: bool = False,
    ) -> GraphBuildSummary:
        resolution = resolve_targets(
            settings=self.settings,
            universities=universities,
            universities_file=None,
            db_roots=db_roots,
        )
        mode = "rebuild" if rebuild else "incremental"
        errors: list[str] = []
        nodes_written = 0
        edges_written = 0
        terms_written = 0
        indexed_sources = 0
        skipped_sources = 0

        async with repository.connect_graph(self.graph_db_path) as conn:
            if rebuild:
                await repository.reset_graph(conn)
            run_id = await repository.start_build_run(conn, mode=mode)
            for source_db in resolution.targets:
                try:
                    content_hash = _hash_file(source_db)
                    mtime = source_db.stat().st_mtime
                    source_path = str(source_db)
                    if not rebuild and await repository.source_is_unchanged(
                        conn,
                        source_path=source_path,
                        content_hash=content_hash,
                    ):
                        skipped_sources += 1
                        continue

                    source_prefix = _source_prefix(source_db)
                    await repository.delete_source_graph(conn, source_prefix=source_prefix)
                    snapshot = await self._read_source(source_db)
                    counts = await self._index_snapshot(
                        conn,
                        source_db=source_db,
                        source_prefix=source_prefix,
                        snapshot=snapshot,
                    )
                    await repository.upsert_source(
                        conn,
                        source_path=source_path,
                        university_name=str(snapshot.meta.get("name") or source_db.stem),
                        db_mtime=mtime,
                        content_hash=content_hash,
                    )
                    indexed_sources += 1
                    nodes_written += counts["nodes"]
                    edges_written += counts["edges"]
                    terms_written += counts["terms"]
                    await conn.commit()
                except Exception as error:
                    self.logger.exception("Knowledge graph build failed source=%s", source_db)
                    errors.append(f"{source_db}: {error}")

            await repository.finish_build_run(
                conn,
                run_id=run_id,
                sources_seen=len(resolution.targets),
                sources_indexed=indexed_sources,
                nodes_written=nodes_written,
                edges_written=edges_written,
                terms_written=terms_written,
                skipped_sources=skipped_sources,
                errors=errors,
            )

        return GraphBuildSummary(
            graph_db_path=str(self.graph_db_path),
            source_count=len(resolution.targets),
            indexed_sources=indexed_sources,
            skipped_sources=skipped_sources,
            nodes_written=nodes_written,
            edges_written=edges_written,
            terms_written=terms_written,
            errors=errors + resolution.unmatched_universities + resolution.unmatched_db_roots,
        )

    async def _index_snapshot(
        self,
        conn: aiosqlite.Connection,
        *,
        source_db: Path,
        source_prefix: str,
        snapshot: _SourceSnapshot,
    ) -> dict[str, int]:
        counts = {"nodes": 0, "edges": 0, "terms": 0}
        university_name = str(snapshot.meta.get("name") or source_db.stem)
        start_url = _clean(snapshot.meta.get("start_url"))
        location = _clean(snapshot.meta.get("location"))
        university_key = f"{source_prefix}:university"
        doc_parts: dict[str, list[str]] = defaultdict(list)
        node_types: dict[str, str] = {}

        async def add_node(
            *,
            node_type: str,
            key: str,
            name: str,
            org_unit_name: str = "",
            source_url: str | None = None,
            payload: dict[str, Any] | None = None,
            document_parts: list[str] | None = None,
        ) -> None:
            counts["nodes"] += await repository.upsert_node(
                conn,
                node_type=node_type,
                source_key=key,
                name=name,
                university_name=university_name,
                org_unit_name=org_unit_name,
                source_url=source_url,
                payload=payload,
            )
            node_types[key] = node_type
            doc_parts[key].extend([part for part in (document_parts or []) if _clean(part)])

        async def add_edge(source_key: str, target_key: str, relation: str, weight: float = 1.0) -> None:
            counts["edges"] += await repository.upsert_edge(
                conn,
                source_node_key=source_key,
                target_node_key=target_key,
                relation=relation,
                weight=weight,
            )

        async def add_concept(term: str) -> str:
            normalized = _clean(term)
            key = concept_key(normalized)
            await add_node(
                node_type="concept",
                key=key,
                name=normalized,
                payload={"term": normalized},
                document_parts=[normalized],
            )
            return key

        await add_node(
            node_type="university",
            key=university_key,
            name=university_name,
            source_url=start_url,
            payload={
                "source_db": str(source_db),
                "university_name": university_name,
                "start_url": start_url,
                "location": location,
            },
            document_parts=[university_name, location, start_url],
        )
        if location:
            location_key = await add_concept(location)
            await add_edge(university_key, location_key, "LOCATED_IN")

        org_keys_by_id: dict[int, str] = {}
        org_keys_by_name: dict[str, str] = {}
        for org in snapshot.org_units:
            org_id = int(org.get("id") or 0)
            org_name = _clean(org.get("name")) or "Unknown"
            org_url = _clean(org.get("url"))
            org_key = f"{source_prefix}:org_unit:{org_id or hashlib.sha1(org_name.encode('utf-8')).hexdigest()[:12]}"
            org_keys_by_id[org_id] = org_key
            org_keys_by_name[org_name] = org_key
            await add_node(
                node_type="org_unit",
                key=org_key,
                name=org_name,
                org_unit_name=org_name,
                source_url=org_url,
                payload={
                    "source_db": str(source_db),
                    "university_key": university_key,
                    "university_name": university_name,
                    "org_unit_id": org_id,
                    "org_unit_name": org_name,
                    "url": org_url,
                    "kind": _clean(org.get("kind")),
                    "location": location,
                },
                document_parts=[university_name, location, org_name, org_url, _clean(org.get("kind"))],
            )
            await add_edge(university_key, org_key, "HAS_ORG_UNIT")

        affiliations_by_professor: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for affiliation in snapshot.affiliations:
            affiliations_by_professor[int(affiliation.get("professor_id") or 0)].append(affiliation)

        for professor in snapshot.professors:
            prof_id = int(professor.get("id") or 0)
            prof_key = f"{source_prefix}:professor:{prof_id}"
            org_unit_name = _clean(professor.get("org_unit_name")) or "Unknown"
            org_keys, source_url = _resolve_professor_orgs(
                professor=professor,
                affiliations=affiliations_by_professor.get(prof_id, []),
                org_keys_by_id=org_keys_by_id,
                org_keys_by_name=org_keys_by_name,
            )
            await self._index_teacher(
                conn,
                add_node=add_node,
                add_edge=add_edge,
                add_concept=add_concept,
                doc_parts=doc_parts,
                university_key=university_key,
                university_name=university_name,
                location=location,
                teacher=professor,
                teacher_key=prof_key,
                entity_type="professor",
                org_unit_name=org_unit_name,
                org_keys=org_keys,
                source_url=source_url,
                source_db=source_db,
            )

        for academician in snapshot.academicians:
            academician_id = int(academician.get("id") or 0)
            academician_key = f"{source_prefix}:academician:{academician_id}"
            org_id = int(academician.get("org_unit_id") or 0)
            org_keys = [org_keys_by_id[org_id]] if org_id in org_keys_by_id else []
            org_name = _org_name_from_key(org_keys[0], snapshot.org_units, org_keys_by_id) if org_keys else "Unknown"
            await self._index_teacher(
                conn,
                add_node=add_node,
                add_edge=add_edge,
                add_concept=add_concept,
                doc_parts=doc_parts,
                university_key=university_key,
                university_name=university_name,
                location=location,
                teacher=academician,
                teacher_key=academician_key,
                entity_type="academician",
                org_unit_name=org_name,
                org_keys=org_keys,
                source_url=_clean(academician.get("source_url")) or _clean(academician.get("homepage")),
                source_db=source_db,
            )

        for node_key, parts in doc_parts.items():
            text = " ".join(part for part in parts if _clean(part))
            node_type = node_types.get(node_key, "concept")
            counts["terms"] += await repository.upsert_document(
                conn,
                node_key=node_key,
                node_type=node_type,
                document_text=text,
                tokens=tokenize(text),
            )
        return counts

    async def _index_teacher(
        self,
        conn: aiosqlite.Connection,
        *,
        add_node: Any,
        add_edge: Any,
        add_concept: Any,
        doc_parts: dict[str, list[str]],
        university_key: str,
        university_name: str,
        location: str,
        teacher: dict[str, Any],
        teacher_key: str,
        entity_type: str,
        org_unit_name: str,
        org_keys: list[str],
        source_url: str,
        source_db: Path,
    ) -> None:
        _ = conn
        name = _clean(teacher.get("name"))
        title = _clean(teacher.get("title"))
        research_areas = _clean(teacher.get("research_areas"))
        enrollment_pref = _clean(teacher.get("enrollment_pref"))
        publications = _clean(teacher.get("publications"))
        bio = _clean(teacher.get("bio"))
        homepage = _clean(teacher.get("homepage"))
        external_link = _clean(teacher.get("external_link"))
        evidence_url = homepage or source_url or external_link
        payload = {
            "source_db": str(source_db),
            "entity_type": entity_type,
            "university_key": university_key,
            "university_name": university_name,
            "location": location,
            "org_unit_keys": org_keys,
            "org_unit_name": org_unit_name,
            "name": name,
            "title": title,
            "research_areas": research_areas,
            "email": _clean(teacher.get("email")),
            "phone": _clean(teacher.get("phone")),
            "homepage": homepage,
            "external_link": external_link,
            "source_url": source_url,
            "bio": bio,
            "enrollment_pref": enrollment_pref,
            "publications": publications,
        }
        document_parts = [
            university_name,
            location,
            org_unit_name,
            name,
            title,
            research_areas,
            enrollment_pref,
            publications,
            bio,
        ]
        await add_node(
            node_type="professor",
            key=teacher_key,
            name=name,
            org_unit_name=org_unit_name,
            source_url=evidence_url,
            payload=payload,
            document_parts=document_parts,
        )
        for org_key in org_keys:
            await add_edge(teacher_key, org_key, "AFFILIATED_WITH")
            doc_parts[org_key].extend([research_areas, enrollment_pref, publications, bio, title])

        for concept in extract_concepts(research_areas, publications, bio, limit=12):
            concept_node = await add_concept(concept)
            await add_edge(teacher_key, concept_node, "RESEARCHES")
        for concept in extract_concepts(enrollment_pref, limit=4):
            concept_node = await add_concept(concept)
            await add_edge(teacher_key, concept_node, "RECRUITS_FOR")
        if title:
            title_node = await add_concept(title)
            await add_edge(teacher_key, title_node, "HAS_TITLE")

    async def _read_source(self, source_db: Path) -> _SourceSnapshot:
        conn = await aiosqlite.connect(source_db)
        conn.row_factory = aiosqlite.Row
        try:
            meta = await _fetch_optional_one(
                conn,
                "university_meta",
                ["name", "start_url", "location"],
            )
            return _SourceSnapshot(
                meta=meta or {"name": source_db.stem, "start_url": "", "location": ""},
                org_units=await _fetch_optional_all(
                    conn,
                    "org_units",
                    ["id", "name", "url", "kind", "status"],
                    order_by="id",
                ),
                professors=await _fetch_optional_all(
                    conn,
                    "professors",
                    [
                        "id",
                        "name",
                        "org_unit_name",
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
                    order_by="id",
                ),
                academicians=await _fetch_optional_all(
                    conn,
                    "academicians",
                    [
                        "id",
                        "name",
                        "title",
                        "research_areas",
                        "email",
                        "phone",
                        "homepage",
                        "external_link",
                        "bio",
                        "enrollment_pref",
                        "publications",
                        "org_unit_id",
                        "source_url",
                    ],
                    order_by="id",
                ),
                affiliations=await _fetch_optional_all(
                    conn,
                    "professor_affiliations",
                    ["professor_id", "org_unit_id", "source_url"],
                    order_by="id",
                ),
            )
        finally:
            await conn.close()


async def _fetch_optional_one(
    conn: aiosqlite.Connection,
    table: str,
    columns: list[str],
) -> dict[str, Any] | None:
    rows = await _fetch_optional_all(conn, table, columns, limit=1)
    return rows[0] if rows else None


async def _fetch_optional_all(
    conn: aiosqlite.Connection,
    table: str,
    columns: list[str],
    *,
    order_by: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    existing = await _table_columns(conn, table)
    if not existing:
        return []
    selected = [column for column in columns if column in existing]
    if not selected:
        return []
    sql = f"SELECT {', '.join(selected)} FROM {table}"
    if order_by and order_by in existing:
        sql += f" ORDER BY {order_by}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    cursor = await conn.execute(sql)
    rows = await cursor.fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = {column: None for column in columns}
        for column in selected:
            item[column] = row[column]
        result.append(item)
    return result


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return {str(row["name"]) for row in rows}


def _resolve_professor_orgs(
    *,
    professor: dict[str, Any],
    affiliations: list[dict[str, Any]],
    org_keys_by_id: dict[int, str],
    org_keys_by_name: dict[str, str],
) -> tuple[list[str], str]:
    org_keys: list[str] = []
    source_url = ""
    for affiliation in affiliations:
        org_id = int(affiliation.get("org_unit_id") or 0)
        org_key = org_keys_by_id.get(org_id)
        if org_key and org_key not in org_keys:
            org_keys.append(org_key)
        if not source_url:
            source_url = _clean(affiliation.get("source_url"))
    org_name = _clean(professor.get("org_unit_name"))
    if not org_keys and org_name in org_keys_by_name:
        org_keys.append(org_keys_by_name[org_name])
    if not source_url:
        source_url = _clean(professor.get("homepage"))
    return org_keys, source_url


def _org_name_from_key(
    org_key: str,
    org_units: list[dict[str, Any]],
    org_keys_by_id: dict[int, str],
) -> str:
    for org in org_units:
        org_id = int(org.get("id") or 0)
        if org_keys_by_id.get(org_id) == org_key:
            return _clean(org.get("name")) or "Unknown"
    return "Unknown"


def _source_prefix(path: Path) -> str:
    digest = hashlib.sha1(str(path.resolve()).lower().encode("utf-8")).hexdigest()[:16]
    return f"source:{digest}"


def _hash_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clean(value: Any) -> str:
    return str(value or "").strip()
