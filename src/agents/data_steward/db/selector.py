from __future__ import annotations

from pathlib import Path

from agents.crawler import db as crawler_db
from agents.crawler.config import CrawlerSettings
from agents.crawler.dispatcher import _university_db_path
from agents.data_steward.types import TargetResolution


def resolve_targets(
    *,
    settings: CrawlerSettings,
    universities: list[str] | None,
    universities_file: Path | None,
    db_roots: list[str] | None,
) -> TargetResolution:
    university_names = _normalize_name_selectors(universities, universities_file)
    roots = _normalize_db_roots(db_roots)
    db_dir = Path(settings.university_db_dir)
    db_dir.mkdir(parents=True, exist_ok=True)

    all_targets = crawler_db.load_university_targets_from_csv(settings.websites_path)
    name_to_db: dict[str, Path] = {}
    lowered_name_to_db: dict[str, Path] = {}
    for item in all_targets:
        db_path = _university_db_path(db_dir, item["url"])
        name = str(item["name"]).strip()
        name_to_db[name] = db_path
        lowered_name_to_db[name.lower()] = db_path

    selected_paths: set[Path] = set()
    unmatched_universities: list[str] = []
    if university_names:
        for name in university_names:
            matched = name_to_db.get(name) or lowered_name_to_db.get(name.lower())
            if matched is None:
                unmatched_universities.append(name)
                continue
            selected_paths.add(matched)

    existing_roots = {path.stem.lower(): path for path in db_dir.glob("*.db")}
    unmatched_db_roots: list[str] = []
    if roots:
        for root in roots:
            stem = root.lower().removesuffix(".db")
            matched = existing_roots.get(stem)
            if matched is None:
                unmatched_db_roots.append(root)
                continue
            selected_paths.add(matched)

    if not university_names and not roots:
        selected_paths = set(db_dir.glob("*.db"))

    active_paths = sorted(path for path in selected_paths if path.is_file())
    return TargetResolution(
        targets=active_paths,
        unmatched_universities=sorted(set(unmatched_universities)),
        unmatched_db_roots=sorted(set(unmatched_db_roots)),
    )


def _normalize_name_selectors(
    universities: list[str] | None,
    universities_file: Path | None,
) -> list[str]:
    names: list[str] = []
    for item in universities or []:
        value = str(item).strip()
        if value:
            names.append(value)
    if universities_file:
        content = universities_file.read_text(encoding="utf-8")
        for line in content.splitlines():
            value = line.strip()
            if value:
                names.append(value)
    unique: list[str] = []
    for item in names:
        if item not in unique:
            unique.append(item)
    return unique


def _normalize_db_roots(db_roots: list[str] | None) -> list[str]:
    roots: list[str] = []
    for item in db_roots or []:
        value = str(item).strip()
        if value:
            roots.append(value)
    unique: list[str] = []
    for item in roots:
        if item not in unique:
            unique.append(item)
    return unique

