from __future__ import annotations

import aiosqlite


SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS kg_sources (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_path TEXT NOT NULL UNIQUE,
        university_name TEXT NOT NULL DEFAULT '',
        db_mtime REAL NOT NULL DEFAULT 0,
        content_hash TEXT NOT NULL DEFAULT '',
        indexed_at TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kg_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_type TEXT NOT NULL,
        source_key TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL DEFAULT '',
        university_name TEXT NOT NULL DEFAULT '',
        org_unit_name TEXT NOT NULL DEFAULT '',
        source_url TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kg_edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_node_key TEXT NOT NULL,
        target_node_key TEXT NOT NULL,
        relation TEXT NOT NULL,
        weight REAL NOT NULL DEFAULT 1.0,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(source_node_key, target_node_key, relation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kg_documents (
        node_key TEXT PRIMARY KEY,
        node_type TEXT NOT NULL,
        document_text TEXT NOT NULL DEFAULT '',
        tokens_json TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kg_terms (
        term TEXT NOT NULL,
        node_key TEXT NOT NULL,
        node_type TEXT NOT NULL,
        tf INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(term, node_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kg_build_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        mode TEXT NOT NULL DEFAULT 'incremental',
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT,
        sources_seen INTEGER NOT NULL DEFAULT 0,
        sources_indexed INTEGER NOT NULL DEFAULT 0,
        nodes_written INTEGER NOT NULL DEFAULT 0,
        edges_written INTEGER NOT NULL DEFAULT 0,
        terms_written INTEGER NOT NULL DEFAULT 0,
        skipped_sources INTEGER NOT NULL DEFAULT 0,
        errors_json TEXT NOT NULL DEFAULT '[]'
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_kg_nodes_type ON kg_nodes(node_type)",
    "CREATE INDEX IF NOT EXISTS ix_kg_nodes_university ON kg_nodes(university_name)",
    "CREATE INDEX IF NOT EXISTS ix_kg_edges_source ON kg_edges(source_node_key)",
    "CREATE INDEX IF NOT EXISTS ix_kg_edges_target ON kg_edges(target_node_key)",
    "CREATE INDEX IF NOT EXISTS ix_kg_edges_relation ON kg_edges(relation)",
    "CREATE INDEX IF NOT EXISTS ix_kg_terms_term ON kg_terms(term)",
    "CREATE INDEX IF NOT EXISTS ix_kg_terms_node ON kg_terms(node_key)",
]


async def ensure_schema(conn: aiosqlite.Connection) -> None:
    for statement in SCHEMA_SQL:
        await conn.execute(statement)
