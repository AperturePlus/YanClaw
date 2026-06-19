from __future__ import annotations

import json
import math
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from agents.recommender.db.schema import ensure_schema
from agents.recommender.text import token_counts


@dataclass(frozen=True)
class NodeSearchScore:
    node_key: str
    node_type: str
    score: float
    matched_terms: list[str]


@asynccontextmanager
async def connect_graph(db_path: str | Path) -> AsyncIterator[aiosqlite.Connection]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    try:
        await ensure_schema(conn)
        yield conn
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        await conn.close()


async def reset_graph(conn: aiosqlite.Connection) -> None:
    for table in ("kg_terms", "kg_documents", "kg_edges", "kg_nodes", "kg_sources"):
        await conn.execute(f"DELETE FROM {table}")


async def start_build_run(conn: aiosqlite.Connection, *, mode: str) -> int:
    cursor = await conn.execute("INSERT INTO kg_build_runs(mode) VALUES (?)", (mode,))
    return int(cursor.lastrowid)


async def finish_build_run(
    conn: aiosqlite.Connection,
    *,
    run_id: int,
    sources_seen: int,
    sources_indexed: int,
    nodes_written: int,
    edges_written: int,
    terms_written: int,
    skipped_sources: int,
    errors: list[str],
) -> None:
    await conn.execute(
        """
        UPDATE kg_build_runs
        SET finished_at = ?,
            sources_seen = ?,
            sources_indexed = ?,
            nodes_written = ?,
            edges_written = ?,
            terms_written = ?,
            skipped_sources = ?,
            errors_json = ?
        WHERE id = ?
        """,
        (
            _now_iso(),
            int(sources_seen),
            int(sources_indexed),
            int(nodes_written),
            int(edges_written),
            int(terms_written),
            int(skipped_sources),
            json.dumps(errors, ensure_ascii=False),
            int(run_id),
        ),
    )


async def source_is_unchanged(
    conn: aiosqlite.Connection,
    *,
    source_path: str,
    content_hash: str,
) -> bool:
    row = await _fetchone(
        conn,
        "SELECT content_hash FROM kg_sources WHERE source_path = ?",
        (source_path,),
    )
    return bool(row and str(row["content_hash"]) == content_hash)


async def upsert_source(
    conn: aiosqlite.Connection,
    *,
    source_path: str,
    university_name: str,
    db_mtime: float,
    content_hash: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO kg_sources(source_path, university_name, db_mtime, content_hash, indexed_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
            university_name = excluded.university_name,
            db_mtime = excluded.db_mtime,
            content_hash = excluded.content_hash,
            indexed_at = excluded.indexed_at
        """,
        (source_path, university_name, float(db_mtime), content_hash, _now_iso()),
    )


async def delete_source_graph(conn: aiosqlite.Connection, *, source_prefix: str) -> None:
    like = f"{source_prefix}:%"
    await conn.execute(
        "DELETE FROM kg_edges WHERE source_node_key LIKE ? OR target_node_key LIKE ?",
        (like, like),
    )
    await conn.execute(
        "DELETE FROM kg_terms WHERE node_key IN (SELECT source_key FROM kg_nodes WHERE source_key LIKE ?)",
        (like,),
    )
    await conn.execute(
        "DELETE FROM kg_documents WHERE node_key IN (SELECT source_key FROM kg_nodes WHERE source_key LIKE ?)",
        (like,),
    )
    await conn.execute("DELETE FROM kg_nodes WHERE source_key LIKE ?", (like,))


async def upsert_node(
    conn: aiosqlite.Connection,
    *,
    node_type: str,
    source_key: str,
    name: str,
    university_name: str = "",
    org_unit_name: str = "",
    source_url: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    await conn.execute(
        """
        INSERT INTO kg_nodes(node_type, source_key, name, university_name, org_unit_name, source_url, payload_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            node_type = excluded.node_type,
            name = excluded.name,
            university_name = excluded.university_name,
            org_unit_name = excluded.org_unit_name,
            source_url = excluded.source_url,
            payload_json = excluded.payload_json
        """,
        (
            node_type,
            source_key,
            name,
            university_name,
            org_unit_name,
            source_url,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        ),
    )
    return 1


async def upsert_edge(
    conn: aiosqlite.Connection,
    *,
    source_node_key: str,
    target_node_key: str,
    relation: str,
    weight: float = 1.0,
    payload: dict[str, Any] | None = None,
) -> int:
    await conn.execute(
        """
        INSERT INTO kg_edges(source_node_key, target_node_key, relation, weight, payload_json)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_node_key, target_node_key, relation) DO UPDATE SET
            weight = excluded.weight,
            payload_json = excluded.payload_json
        """,
        (
            source_node_key,
            target_node_key,
            relation,
            float(weight),
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        ),
    )
    return 1


async def upsert_document(
    conn: aiosqlite.Connection,
    *,
    node_key: str,
    node_type: str,
    document_text: str,
    tokens: list[str],
) -> int:
    await conn.execute(
        """
        INSERT INTO kg_documents(node_key, node_type, document_text, tokens_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(node_key) DO UPDATE SET
            node_type = excluded.node_type,
            document_text = excluded.document_text,
            tokens_json = excluded.tokens_json,
            updated_at = excluded.updated_at
        """,
        (
            node_key,
            node_type,
            document_text,
            json.dumps(tokens, ensure_ascii=False),
            _now_iso(),
        ),
    )
    await conn.execute("DELETE FROM kg_terms WHERE node_key = ?", (node_key,))
    counts = token_counts(tokens)
    if counts:
        await conn.executemany(
            "INSERT INTO kg_terms(term, node_key, node_type, tf) VALUES (?, ?, ?, ?)",
            [(term, node_key, node_type, int(tf)) for term, tf in counts.items()],
        )
    return len(counts)


async def graph_has_documents(conn: aiosqlite.Connection) -> bool:
    row = await _fetchone(conn, "SELECT COUNT(*) AS count FROM kg_documents", ())
    return bool(row and int(row["count"] or 0) > 0)


async def search_by_terms(
    conn: aiosqlite.Connection,
    terms: list[str],
    *,
    limit: int = 200,
) -> list[NodeSearchScore]:
    unique_terms = []
    for term in terms:
        value = str(term or "").strip().lower()
        if value and value not in unique_terms:
            unique_terms.append(value)
    if not unique_terms:
        return []

    count_row = await _fetchone(conn, "SELECT COUNT(*) AS count FROM kg_documents", ())
    doc_count = max(1, int(count_row["count"] if count_row else 1))
    scores: dict[str, float] = defaultdict(float)
    node_types: dict[str, str] = {}
    matched: dict[str, set[str]] = defaultdict(set)
    for term in unique_terms:
        rows = await _fetchall(
            conn,
            "SELECT node_key, node_type, tf FROM kg_terms WHERE term = ?",
            (term,),
        )
        if not rows:
            continue
        df = len(rows)
        idf = math.log((doc_count + 1) / (df + 1)) + 1.0
        for row in rows:
            node_key = str(row["node_key"])
            node_type = str(row["node_type"])
            tf = max(1, int(row["tf"] or 1))
            scores[node_key] += float(tf) * idf
            node_types[node_key] = node_type
            matched[node_key].add(term)

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
    return [
        NodeSearchScore(
            node_key=node_key,
            node_type=node_types.get(node_key, ""),
            score=round(score, 6),
            matched_terms=sorted(matched[node_key]),
        )
        for node_key, score in ordered
    ]


async def get_nodes(
    conn: aiosqlite.Connection,
    node_keys: list[str],
) -> dict[str, dict[str, Any]]:
    unique = [key for index, key in enumerate(node_keys) if key and key not in node_keys[:index]]
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    rows = await _fetchall(
        conn,
        f"SELECT * FROM kg_nodes WHERE source_key IN ({placeholders})",
        tuple(unique),
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        try:
            item["payload"] = json.loads(str(item.get("payload_json") or "{}"))
        except json.JSONDecodeError:
            item["payload"] = {}
        result[str(item["source_key"])] = item
    return result


async def find_professors_linked_to_concepts(
    conn: aiosqlite.Connection,
    concept_keys: list[str],
    *,
    limit: int = 200,
) -> dict[str, list[str]]:
    unique = [key for index, key in enumerate(concept_keys) if key and key not in concept_keys[:index]]
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    rows = await _fetchall(
        conn,
        f"""
        SELECT source_node_key, target_node_key
        FROM kg_edges
        WHERE target_node_key IN ({placeholders})
          AND relation IN ('RESEARCHES', 'RECRUITS_FOR', 'HAS_TITLE')
        LIMIT ?
        """,
        (*unique, int(limit)),
    )
    result: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        result[str(row["source_node_key"])].append(str(row["target_node_key"]))
    return dict(result)


async def count_rows(conn: aiosqlite.Connection, table: str) -> int:
    row = await _fetchone(conn, f"SELECT COUNT(*) AS count FROM {table}", ())
    return int(row["count"] if row else 0)


async def _fetchone(
    conn: aiosqlite.Connection,
    sql: str,
    params: tuple[Any, ...],
) -> aiosqlite.Row | None:
    cursor = await conn.execute(sql, params)
    return await cursor.fetchone()


async def _fetchall(
    conn: aiosqlite.Connection,
    sql: str,
    params: tuple[Any, ...],
) -> list[aiosqlite.Row]:
    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return list(rows)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
