from __future__ import annotations

from dataclasses import dataclass, field
import os
import sqlite3
from pathlib import Path
from uuid import uuid4


PUBLIC_TABLES: tuple[str, ...] = (
    "university_meta",
    "org_units",
    "professors",
    "academicians",
    "professor_affiliations",
)


@dataclass(frozen=True)
class CleanExportResult:
    source_db: Path
    export_path: Path
    size_bytes: int
    row_counts: dict[str, int] = field(default_factory=dict)


def export_clean_database(source_db: Path, export_root: Path) -> CleanExportResult:
    source = Path(source_db)
    if not source.is_file():
        raise FileNotFoundError(f"source DB does not exist: {source}")

    root = Path(export_root)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{source.stem}.clean.db"
    temp_path = root / f".{source.stem}.{uuid4().hex}.tmp.db"

    source_conn: sqlite3.Connection | None = None
    dest_conn: sqlite3.Connection | None = None
    try:
        source_conn = _connect_source_readonly(source)
        source_conn.row_factory = sqlite3.Row
        missing_tables = [table for table in PUBLIC_TABLES if not _table_exists(source_conn, table)]
        if missing_tables:
            raise sqlite3.DatabaseError("source DB missing required tables: " + ", ".join(missing_tables))

        dest_conn = sqlite3.connect(temp_path)
        dest_conn.execute("PRAGMA foreign_keys = ON")
        _create_clean_schema(dest_conn)
        row_counts = _copy_public_tables(source_conn, dest_conn)
        dest_conn.commit()
        _validate_clean_database(dest_conn)
        dest_conn.execute("PRAGMA optimize")
        dest_conn.execute("VACUUM")
        dest_conn.close()
        dest_conn = None

        os.replace(temp_path, destination)
        return CleanExportResult(
            source_db=source,
            export_path=destination,
            size_bytes=destination.stat().st_size,
            row_counts=row_counts,
        )
    except Exception:
        if dest_conn is not None:
            dest_conn.close()
        if temp_path.exists():
            temp_path.unlink()
        raise
    finally:
        if source_conn is not None:
            source_conn.close()


def default_export_root(university_db_dir: Path) -> Path:
    return Path(university_db_dir).parent / "exports" / "universities"


def _connect_source_readonly(source: Path) -> sqlite3.Connection:
    uri = source.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _create_clean_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;

        CREATE TABLE university_meta (
            id INTEGER PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            start_url TEXT NOT NULL DEFAULT '',
            location VARCHAR(255) NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX ix_university_meta_name ON university_meta(name);

        CREATE TABLE org_units (
            id INTEGER PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            url TEXT NOT NULL,
            kind VARCHAR(64)
        );
        CREATE UNIQUE INDEX uq_org_unit_name ON org_units(name);
        CREATE UNIQUE INDEX uq_org_unit_url ON org_units(url);
        CREATE INDEX ix_org_units_name ON org_units(name);

        CREATE TABLE professors (
            id INTEGER PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            name_key VARCHAR(255) NOT NULL DEFAULT '',
            org_unit_name VARCHAR(255) NOT NULL DEFAULT 'Unknown',
            title VARCHAR(255),
            research_areas TEXT,
            email VARCHAR(255),
            phone VARCHAR(255),
            homepage TEXT,
            external_link TEXT,
            bio TEXT,
            enrollment_pref TEXT,
            publications TEXT
        );
        CREATE INDEX ix_professors_name ON professors(name);
        CREATE INDEX ix_professors_name_key ON professors(name_key);
        CREATE INDEX ix_professors_org_unit_name ON professors(org_unit_name);
        CREATE INDEX ix_professors_email ON professors(email);

        CREATE TABLE academicians (
            id INTEGER PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            name_key VARCHAR(255) NOT NULL DEFAULT '',
            title VARCHAR(255),
            research_areas TEXT,
            email VARCHAR(255),
            phone VARCHAR(255),
            homepage TEXT,
            external_link TEXT,
            bio TEXT,
            enrollment_pref TEXT,
            publications TEXT,
            org_unit_id INTEGER NOT NULL,
            source_url TEXT,
            FOREIGN KEY(org_unit_id) REFERENCES org_units(id)
        );
        CREATE UNIQUE INDEX uq_academician_name_org_unit ON academicians(name, org_unit_id);
        CREATE UNIQUE INDEX uq_academician_org_unit_name_key ON academicians(org_unit_id, name_key);
        CREATE INDEX ix_academicians_name ON academicians(name);
        CREATE INDEX ix_academicians_name_key ON academicians(name_key);
        CREATE INDEX ix_academicians_email ON academicians(email);
        CREATE INDEX ix_academicians_org_unit_id ON academicians(org_unit_id);

        CREATE TABLE professor_affiliations (
            id INTEGER PRIMARY KEY,
            professor_id INTEGER NOT NULL,
            org_unit_id INTEGER NOT NULL,
            source_url TEXT,
            FOREIGN KEY(professor_id) REFERENCES professors(id),
            FOREIGN KEY(org_unit_id) REFERENCES org_units(id)
        );
        CREATE UNIQUE INDEX uq_professor_affiliation
            ON professor_affiliations(professor_id, org_unit_id);
        CREATE INDEX ix_professor_affiliations_professor_id
            ON professor_affiliations(professor_id);
        CREATE INDEX ix_professor_affiliations_org_unit_id
            ON professor_affiliations(org_unit_id);
        """
    )


def _copy_public_tables(source: sqlite3.Connection, dest: sqlite3.Connection) -> dict[str, int]:
    row_counts: dict[str, int] = {}
    for table in PUBLIC_TABLES:
        columns = _table_columns(dest, table)
        quoted = ", ".join(_quote_identifier(column) for column in columns)
        rows = source.execute(f"SELECT {quoted} FROM {_quote_identifier(table)}").fetchall()
        if rows:
            placeholders = ", ".join("?" for _ in columns)
            dest.executemany(
                f"INSERT INTO {_quote_identifier(table)} ({quoted}) VALUES ({placeholders})",
                ([row[column] for column in columns] for row in rows),
            )
        row_counts[table] = len(rows)
    return row_counts


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({_quote_identifier(table)})").fetchall()
    return [str(row[1]) for row in rows]


def _validate_clean_database(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if tables != set(PUBLIC_TABLES):
        unexpected = sorted(tables - set(PUBLIC_TABLES))
        missing = sorted(set(PUBLIC_TABLES) - tables)
        details = []
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        if missing:
            details.append("missing=" + ",".join(missing))
        raise sqlite3.DatabaseError("clean export schema mismatch: " + " ".join(details))

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise sqlite3.IntegrityError(f"clean export foreign key violations: {violations[:3]}")

    integrity = conn.execute("PRAGMA integrity_check").fetchone()
    if not integrity or str(integrity[0]).lower() != "ok":
        raise sqlite3.DatabaseError(f"clean export integrity check failed: {integrity}")


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


__all__ = [
    "CleanExportResult",
    "PUBLIC_TABLES",
    "default_export_root",
    "export_clean_database",
]
