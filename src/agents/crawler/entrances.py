from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from agents.crawler.sanitizer import normalize_org_unit_name
from agents.crawler.url_heuristics import _sanitize_url


class EntranceManifestError(ValueError):
    """Raised when the crawler entrance manifest is malformed."""


@dataclass(frozen=True)
class ManualOrgUnitEntrance:
    name: str
    url: str = ""
    faculty_url: str = ""
    kind: str = "college"
    raw_name: str = ""
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class UniversityEntranceTarget:
    name: str
    url: str
    location: str = ""
    org_unit_listing_urls: tuple[str, ...] = ()
    manual_org_units: tuple[ManualOrgUnitEntrance, ...] = ()

    @property
    def has_manual_entrances(self) -> bool:
        return bool(self.org_unit_listing_urls or self.manual_org_units)


_RECORD_TYPE_KEYS = ("record_type", "type", "记录类型", "类型")
_UNIVERSITY_NAME_KEYS = (
    "university",
    "university_name",
    "name",
    "大学名称",
    "高校名称",
    "学校名称",
)
_UNIVERSITY_URL_KEYS = (
    "url",
    "start_url",
    "website",
    "homepage",
    "官网地址",
    "高校官网",
    "学校官网",
)
_LOCATION_KEYS = ("location", "city", "所在地", "城市")
_ORG_LISTING_URL_KEYS = (
    "org_unit_listing_url",
    "org_unit_listing_urls",
    "org_listing_url",
    "org_listing",
    "学院列表入口",
    "院系列表入口",
    "学院入口",
    "院系入口",
)
_ORG_UNIT_NAME_KEYS = (
    "org_unit_name",
    "college_name",
    "school_name",
    "department_name",
    "学院名称",
    "院系名称",
    "单位名称",
)
_ORG_UNIT_URL_KEYS = (
    "org_unit_url",
    "college_url",
    "school_url",
    "department_url",
    "学院主页",
    "院系主页",
)
_FACULTY_URL_KEYS = (
    "faculty_url",
    "faculty_urls",
    "faculty_entrance",
    "teacher_url",
    "teachers_url",
    "师资入口",
    "师资URL",
    "教师入口",
    "教师列表",
)
_ORG_UNIT_KIND_KEYS = ("kind", "org_unit_kind", "学院类型", "单位类型")


def load_university_entrance_targets(path: str | Path) -> list[UniversityEntranceTarget]:
    manifest_path = Path(path)
    suffix = manifest_path.suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return _load_yaml_manifest(manifest_path)
    if suffix == ".txt":
        raise EntranceManifestError(
            f"Text entrance manifest is no longer parsed: {manifest_path}. "
            "Convert the raw notes to assets/entrances.yaml first."
        )
    return _load_csv_manifest(manifest_path)


def load_university_targets_from_csv(path: str | Path) -> list[dict[str, object]]:
    """Compatibility wrapper used by older code paths.

    Despite the historical name, this accepts the new YAML manifest and the
    legacy CSV-style manifest.
    """

    targets = load_university_entrance_targets(path)
    result: list[dict[str, object]] = []
    for target in targets:
        result.append(
            {
                "name": target.name,
                "url": target.url,
                "location": target.location,
                "org_unit_listing_urls": list(target.org_unit_listing_urls),
                "manual_org_units": [asdict(unit) for unit in target.manual_org_units],
            }
        )
    return result


def _load_yaml_manifest(path: Path) -> list[UniversityEntranceTarget]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise EntranceManifestError(f"Entrance manifest not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise EntranceManifestError(f"Invalid YAML entrance manifest {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise EntranceManifestError("YAML entrance manifest must be a mapping")
    if data.get("version") != 1:
        raise EntranceManifestError("YAML entrance manifest requires version: 1")
    universities = data.get("universities")
    if not isinstance(universities, list):
        raise EntranceManifestError("YAML entrance manifest requires universities: [...]")

    targets: list[UniversityEntranceTarget] = []
    seen_universities: set[str] = set()
    for index, item in enumerate(universities, start=1):
        context = f"universities[{index}]"
        if not isinstance(item, dict):
            raise EntranceManifestError(f"{context} must be a mapping")
        name = _required_text(item, "name", context)
        if name in seen_universities:
            raise EntranceManifestError(f"Duplicate university name in YAML manifest: {name}")
        seen_universities.add(name)
        url = _required_url(item.get("url"), f"{context}.url")
        location = _optional_text(item.get("location"))

        listing_urls = _optional_url_list(
            item.get("org_unit_listing_urls"),
            f"{context}.org_unit_listing_urls",
        )
        org_units = _parse_yaml_org_units(
            item.get("org_units"),
            university_name=name,
            context=f"{context}.org_units",
        )
        targets.append(
            UniversityEntranceTarget(
                name=name,
                url=url,
                location=location,
                org_unit_listing_urls=() if org_units else tuple(listing_urls),
                manual_org_units=tuple(org_units),
            )
        )
    return targets


def _parse_yaml_org_units(
    value: Any,
    *,
    university_name: str,
    context: str,
) -> list[ManualOrgUnitEntrance]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise EntranceManifestError(f"{context} must be a list when provided")

    result: list[ManualOrgUnitEntrance] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(value, start=1):
        item_context = f"{context}[{index}]"
        if not isinstance(item, dict):
            raise EntranceManifestError(f"{item_context} must be a mapping")
        name = normalize_org_unit_name(_required_text(item, "name", item_context), default="")
        if not name:
            raise EntranceManifestError(f"{item_context}.name is required")
        kind = _optional_text(item.get("kind")) or "college"
        org_url = _optional_url(item.get("url"), f"{item_context}.url")
        faculty_urls = _optional_url_list(item.get("faculty_urls"), f"{item_context}.faculty_urls")
        if not org_url and not faculty_urls:
            raise EntranceManifestError(
                f"{item_context} requires url or faculty_urls for university {university_name}"
            )

        if not faculty_urls:
            faculty_urls = [""]
        for faculty_url in faculty_urls:
            signature = (name, faculty_url)
            if signature in seen:
                raise EntranceManifestError(
                    f"Duplicate org_unit/faculty_url in YAML manifest: {university_name} {name} {faculty_url}"
                )
            seen.add(signature)
            result.append(
                ManualOrgUnitEntrance(
                    name=name,
                    url=org_url,
                    faculty_url=faculty_url,
                    kind=kind,
                    raw_name="",
                    aliases=(),
                )
            )
    return result


def _load_csv_manifest(path: Path) -> list[UniversityEntranceTarget]:
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise EntranceManifestError(f"Entrance manifest not found: {path}") from exc

    lines = [line for line in raw_lines if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        return []
    reader = csv.DictReader(lines)
    if not reader.fieldnames:
        raise EntranceManifestError(f"CSV entrance manifest has no header: {path}")

    builders: dict[str, _TargetBuilder] = {}
    order: list[str] = []
    for row_index, row in enumerate(reader, start=2):
        record_type = _record_type(row)
        university_name = _clean_text(_row_value(row, *_UNIVERSITY_NAME_KEYS, fallback_index=1))
        if not university_name:
            raise EntranceManifestError(f"CSV row {row_index} missing university name")

        builder = builders.get(university_name)
        if builder is None:
            university_url = _optional_url(
                _row_value(row, *_UNIVERSITY_URL_KEYS, fallback_index=2),
                f"CSV row {row_index} university url",
            )
            if not university_url:
                raise EntranceManifestError(f"CSV row {row_index} missing university url for {university_name}")
            builder = _TargetBuilder(
                name=university_name,
                url=university_url,
                location=_clean_text(_row_value(row, *_LOCATION_KEYS, fallback_index=3)),
            )
            builders[university_name] = builder
            order.append(university_name)
        else:
            incoming_url = _optional_url(
                _row_value(row, *_UNIVERSITY_URL_KEYS),
                f"CSV row {row_index} university url",
            )
            if incoming_url and not builder.url:
                builder.url = incoming_url
            incoming_location = _clean_text(_row_value(row, *_LOCATION_KEYS))
            if incoming_location and not builder.location:
                builder.location = incoming_location

        if record_type == "university":
            continue
        if record_type == "org_listing":
            listing_url = _required_url(
                _row_value(row, *_ORG_LISTING_URL_KEYS),
                f"CSV row {row_index} org_unit_listing_url",
            )
            builder.add_listing_url(listing_url)
            continue
        if record_type == "org_unit":
            org_name = normalize_org_unit_name(_row_value(row, *_ORG_UNIT_NAME_KEYS), default="")
            if not org_name:
                raise EntranceManifestError(f"CSV row {row_index} missing org_unit_name")
            org_url = _optional_url(_row_value(row, *_ORG_UNIT_URL_KEYS), f"CSV row {row_index} org_unit_url")
            faculty_urls = _split_csv_url_values(_row_value(row, *_FACULTY_URL_KEYS), f"CSV row {row_index}")
            if not org_url and not faculty_urls:
                raise EntranceManifestError(
                    f"CSV row {row_index} org_unit {org_name} requires org_unit_url or faculty_url"
                )
            kind = _clean_text(_row_value(row, *_ORG_UNIT_KIND_KEYS)) or "college"
            if not faculty_urls:
                faculty_urls = [""]
            for faculty_url in faculty_urls:
                builder.add_manual_org_unit(
                    ManualOrgUnitEntrance(
                        name=org_name,
                        url=org_url,
                        faculty_url=faculty_url,
                        kind=kind,
                        raw_name="",
                        aliases=(),
                    ),
                    row_index=row_index,
                )
            continue
        raise EntranceManifestError(f"CSV row {row_index} has unsupported record_type: {record_type}")

    return [builders[name].build() for name in order]


@dataclass
class _TargetBuilder:
    name: str
    url: str
    location: str = ""
    listing_urls: list[str] | None = None
    manual_org_units: list[ManualOrgUnitEntrance] | None = None
    _manual_signatures: set[tuple[str, str]] | None = None

    def __post_init__(self) -> None:
        self.listing_urls = [] if self.listing_urls is None else self.listing_urls
        self.manual_org_units = [] if self.manual_org_units is None else self.manual_org_units
        self._manual_signatures = set() if self._manual_signatures is None else self._manual_signatures

    def add_listing_url(self, url: str) -> None:
        assert self.listing_urls is not None
        if url not in self.listing_urls:
            self.listing_urls.append(url)

    def add_manual_org_unit(self, item: ManualOrgUnitEntrance, *, row_index: int) -> None:
        assert self.manual_org_units is not None
        assert self._manual_signatures is not None
        signature = (item.name, item.faculty_url)
        if signature in self._manual_signatures:
            raise EntranceManifestError(
                f"Duplicate org_unit/faculty_url in CSV manifest row {row_index}: {item.name} {item.faculty_url}"
            )
        self._manual_signatures.add(signature)
        self.manual_org_units.append(item)

    def build(self) -> UniversityEntranceTarget:
        assert self.listing_urls is not None
        assert self.manual_org_units is not None
        manual_units = tuple(self.manual_org_units)
        return UniversityEntranceTarget(
            name=self.name,
            url=self.url,
            location=self.location,
            org_unit_listing_urls=() if manual_units else tuple(self.listing_urls),
            manual_org_units=manual_units,
        )


def _record_type(row: dict[str, Any]) -> str:
    raw = _clean_text(_row_value(row, *_RECORD_TYPE_KEYS)).lower()
    if raw in {"", "target"}:
        if _row_value(row, *_ORG_UNIT_NAME_KEYS):
            return "org_unit"
        if _row_value(row, *_ORG_LISTING_URL_KEYS):
            return "org_listing"
        return "university"
    if raw in {"university", "大学", "高校", "学校"}:
        return "university"
    if raw in {"org_listing", "org-unit-listing", "listing", "学院列表", "院系列表"}:
        return "org_listing"
    if raw in {"org_unit", "org-unit", "college", "school", "department", "学院", "院系"}:
        return "org_unit"
    return raw


def _row_value(row: dict[str, Any], *keys: str, fallback_index: int | None = None) -> str:
    normalized = {_normalize_header(key): value for key, value in row.items() if key is not None}
    for key in keys:
        value = normalized.get(_normalize_header(key))
        if value is not None and str(value).strip():
            return str(value)
    if fallback_index is not None:
        values = list(row.values())
        if fallback_index < len(values) and values[fallback_index] is not None:
            return str(values[fallback_index])
    return ""


def _normalize_header(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "_")


def _required_text(mapping: dict[str, Any], key: str, context: str) -> str:
    value = _optional_text(mapping.get(key))
    if not value:
        raise EntranceManifestError(f"{context}.{key} is required")
    return value


def _optional_text(value: Any) -> str:
    return _clean_text(value)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().strip("<>").strip()


def _required_url(value: Any, context: str) -> str:
    url = _optional_url(value, context)
    if not url:
        raise EntranceManifestError(f"{context} is required")
    return url


def _optional_url(value: Any, context: str) -> str:
    text = _clean_url(value)
    if not text:
        return ""
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise EntranceManifestError(f"{context} must be an absolute http(s) URL: {text}")
    return text


def _clean_url(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.startswith("[") and "](" in text and text.endswith(")"):
        text = text.split("](", 1)[1][:-1].strip()
    return _sanitize_url(text)


def _optional_url_list(value: Any, context: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise EntranceManifestError(f"{context} must be a list")
    result: list[str] = []
    for index, item in enumerate(value, start=1):
        url = _required_url(item, f"{context}[{index}]")
        if url in result:
            raise EntranceManifestError(f"Duplicate URL in {context}: {url}")
        result.append(url)
    return result


def _split_csv_url_values(value: Any, context: str) -> list[str]:
    text = _clean_text(value)
    if not text:
        return []
    parts = [part.strip() for part in text.replace("，", "\n").replace("；", "\n").replace(";", "\n").split()]
    result: list[str] = []
    for index, part in enumerate(parts, start=1):
        url = _required_url(part, f"{context} faculty_url[{index}]")
        if url in result:
            raise EntranceManifestError(f"Duplicate faculty_url in {context}: {url}")
        result.append(url)
    return result
