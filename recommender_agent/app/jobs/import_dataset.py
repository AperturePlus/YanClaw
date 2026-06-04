import sqlite3
import json
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import Item, OrgUnit, init_db
from app.models.base import engine as target_engine

settings = get_settings()


def _source_db_path() -> Path:
    source = Path(settings.SCU_SOURCE_DB)
    if source.is_absolute():
        return source
    return Path.cwd() / source


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _metadata(row: sqlite3.Row) -> str:
    return json.dumps(
        {
            "email": _text(row["email"]),
            "phone": _text(row["phone"]),
            "homepage": _text(row["homepage"]),
            "enrollment_pref": _text(row["enrollment_pref"]),
            "publications": _text(row["publications"]),
            "source": "scu.edu.cn.db",
        },
        ensure_ascii=False,
    )


def import_scu_data() -> dict[str, int]:
    """Import professor and org_unit data from scu.edu.cn.db into app.db."""
    init_db()
    source_path = _source_db_path()
    if not source_path.exists():
        raise FileNotFoundError(f"SCU source DB not found: {source_path}")

    con = sqlite3.connect(source_path)
    con.row_factory = sqlite3.Row
    try:
        org_units = con.execute(
            "SELECT id, name, url, kind, status FROM org_units WHERE COALESCE(status, 'active') = 'active'"
        ).fetchall()
        professors = con.execute(
            """SELECT id, name, org_unit_name, title, research_areas,
                      email, phone, homepage, bio, enrollment_pref, publications
               FROM professors
               WHERE name IS NOT NULL AND TRIM(name) != ''"""
        ).fetchall()
    finally:
        con.close()

    imported_org_units = 0
    imported_items = 0
    updated_items = 0

    with Session(target_engine) as session:
        existing_org_names = {org.name for org in session.exec(select(OrgUnit)).all()}
        for row in org_units:
            name = _text(row["name"])
            if not name or name in existing_org_names:
                continue
            session.add(
                OrgUnit(
                    name=name,
                    url=_text(row["url"]) or "",
                    kind=_text(row["kind"]),
                    status=_text(row["status"]) or "active",
                )
            )
            existing_org_names.add(name)
            imported_org_units += 1

        existing_by_external_id = {
            item.external_id: item
            for item in session.exec(select(Item).where(Item.external_id.is_not(None))).all()
        }
        existing_by_title_org = {
            (item.title, item.org_unit): item
            for item in session.exec(select(Item)).all()
        }

        for row in professors:
            name = _text(row["name"])
            if not name:
                continue
            org_unit = _text(row["org_unit_name"])
            item = existing_by_external_id.get(row["id"]) or existing_by_title_org.get((name, org_unit))
            is_new = item is None
            if item is None:
                item = Item(external_id=row["id"], title=name)
                session.add(item)

            item.external_id = row["id"]
            item.title = name
            item.category = org_unit
            item.description = _text(row["bio"])
            item.research_areas = _text(row["research_areas"])
            item.org_unit = org_unit
            item.tags = _text(row["title"])
            item.metadata_json = _metadata(row)
            item.is_active = True
            if item.popularity is None:
                item.popularity = 0.5

            if is_new:
                imported_items += 1
            else:
                updated_items += 1

        session.commit()

    result = {
        "source_professors": len(professors),
        "source_org_units": len(org_units),
        "imported_org_units": imported_org_units,
        "imported_items": imported_items,
        "updated_items": updated_items,
    }
    print(result)
    return result


if __name__ == "__main__":
    import_scu_data()
