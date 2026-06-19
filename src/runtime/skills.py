from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    version: int
    path: Path
    applies_to: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    priority: int = 100
    token_budget: int | None = None


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    version: int
    path: Path
    body: str
    applies_to: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    priority: int
    token_budget: int | None


@dataclass(frozen=True)
class CompiledSkillSet:
    specs: tuple[SkillSpec, ...]
    rendered_text: str
    allowed_tools: tuple[str, ...]

    def __str__(self) -> str:
        return self.rendered_text


class SkillManager:
    """Markdown skill loader."""

    def __init__(self, skills_dir: Path, db: object | None = None, agent_name: str = "") -> None:
        _ = (db, agent_name)
        self.skills_dir = Path(skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def list_skills(self) -> list[SkillMeta]:
        metas: list[SkillMeta] = []
        for path in sorted(self.skills_dir.glob("*.md")):
            spec = self._load_spec_from_path(path)
            metas.append(
                SkillMeta(
                    name=spec.name,
                    description=spec.description,
                    version=spec.version,
                    path=spec.path,
                    applies_to=spec.applies_to,
                    allowed_tools=spec.allowed_tools,
                    priority=spec.priority,
                    token_budget=spec.token_budget,
                )
            )
        return metas

    def load_skill(self, name: str) -> str:
        return self._skill_path(name).read_text(encoding="utf-8")

    def load_skills(self, names: list[str]) -> dict[str, str]:
        return {name: self.load_skill(name) for name in names}

    def select_for_state(
        self,
        state: str,
        allowed_tools: set[str] | list[str] | tuple[str, ...] | None = None,
    ) -> CompiledSkillSet:
        state_value = _normalize_scalar(state)
        runtime_tools = {_normalize_scalar(item) for item in (allowed_tools or set()) if _normalize_scalar(item)}
        selected: list[SkillSpec] = []
        for path in sorted(self.skills_dir.glob("*.md")):
            spec = self._load_spec_from_path(path)
            if not _skill_applies_to_state(spec, state_value):
                continue
            spec_tools = set(spec.allowed_tools)
            if spec_tools and not spec_tools.issubset(runtime_tools):
                continue
            selected.append(spec)

        selected.sort(key=lambda item: (item.priority, item.name))
        rendered = "\n\n".join(self._render_spec(spec) for spec in selected if spec.body.strip())
        declared_tools = sorted({tool for spec in selected for tool in spec.allowed_tools if tool in runtime_tools})
        return CompiledSkillSet(
            specs=tuple(selected),
            rendered_text=rendered,
            allowed_tools=tuple(declared_tools),
        )

    async def create_skill(
        self,
        name: str,
        content: str,
        description: str,
        change_summary: str = "Initial version",
    ) -> None:
        path = self._skill_path(name)
        if path.exists():
            raise FileExistsError(f"Skill already exists: {name}")
        now = datetime.now(timezone.utc)
        normalized = self._with_frontmatter(
            name=name,
            content=content,
            description=description,
            version=1,
            created_at=now,
            updated_at=now,
        )
        path.write_text(normalized, encoding="utf-8")

    def _skill_path(self, name: str) -> Path:
        if "/" in name or "\\" in name or name in {"", ".", ".."}:
            raise ValueError(f"Invalid skill name: {name!r}")
        return self.skills_dir / f"{name}.md"

    def _load_spec_from_path(self, path: Path) -> SkillSpec:
        content = path.read_text(encoding="utf-8")
        frontmatter, body = parse_frontmatter(content)
        name = str(frontmatter.get("name") or path.stem)
        description = str(frontmatter.get("description") or "")
        version = _parse_int(frontmatter.get("version"), default=1)
        applies_to = _parse_csv_tuple(frontmatter.get("applies_to"))
        allowed_tools = _parse_csv_tuple(frontmatter.get("allowed_tools"))
        priority = _parse_int(frontmatter.get("priority"), default=100)
        token_budget_raw = frontmatter.get("token_budget")
        token_budget = _parse_int(token_budget_raw, default=0) if token_budget_raw else None
        if token_budget is not None and token_budget <= 0:
            token_budget = None
        if not applies_to or not allowed_tools:
            inferred_applies_to, inferred_allowed_tools = _infer_skill_scope(name)
            applies_to = applies_to or inferred_applies_to
            allowed_tools = allowed_tools or inferred_allowed_tools
        return SkillSpec(
            name=name,
            description=description,
            version=version,
            path=path,
            body=body.strip(),
            applies_to=applies_to,
            allowed_tools=allowed_tools,
            priority=priority,
            token_budget=token_budget,
        )

    def _render_spec(self, spec: SkillSpec) -> str:
        body = _apply_token_budget(spec.body, spec.token_budget)
        parts = [
            f"## Skill: {spec.name}",
            f"Description: {spec.description}" if spec.description else "",
            f"Version: {spec.version}",
            f"Applies to: {', '.join(spec.applies_to) if spec.applies_to else '*'}",
            f"Allowed tools: {', '.join(spec.allowed_tools) if spec.allowed_tools else '(none)'}",
            "Instructions:",
            body,
        ]
        return "\n".join(part for part in parts if part).strip()

    def _with_frontmatter(
        self,
        *,
        name: str,
        content: str,
        description: str,
        version: int,
        created_at: datetime,
        updated_at: datetime,
    ) -> str:
        frontmatter, body = parse_frontmatter(content)
        description = str(frontmatter.get("description") or description)
        created = frontmatter.get("created_at") or created_at.isoformat(timespec="seconds")
        header = "\n".join(
            [
                "---",
                f"name: {name}",
                f"description: {description}",
                f"version: {version}",
                *[
                    f"{key}: {frontmatter[key]}"
                    for key in ("applies_to", "allowed_tools", "priority", "token_budget")
                    if key in frontmatter
                ],
                f"created_at: {created}",
                f"updated_at: {updated_at.isoformat(timespec='seconds')}",
                "---",
                "",
            ]
        )
        return header + body.lstrip()


def parse_frontmatter(content: str) -> tuple[dict[str, str], str]:
    if not content.startswith("---"):
        return {}, content

    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, content

    end_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}, content

    data: dict[str, str] = {}
    for line in lines[1:end_index]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"').strip("'")
    body = "\n".join(lines[end_index + 1 :])
    if content.endswith("\n"):
        body += "\n"
    return data, body


def _parse_int(value: object, *, default: int) -> int:
    try:
        return int(str(value))
    except Exception:
        return default


def _normalize_scalar(value: object) -> str:
    return str(value or "").strip()


def _parse_csv_tuple(value: object) -> tuple[str, ...]:
    raw = _normalize_scalar(value)
    if not raw:
        return ()
    normalized = raw.replace("|", ",").replace(";", ",")
    items = []
    for item in normalized.split(","):
        cleaned = item.strip()
        if cleaned:
            items.append(cleaned)
    return tuple(dict.fromkeys(items))


def _infer_skill_scope(name: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    normalized = name.strip().lower()
    if normalized == "extract-links":
        return (
            ("DISCOVER_ORG_UNIT_PAGES", "FIND_FACULTY_PAGES"),
            ("extract_links",),
        )
    if normalized == "save-professors":
        return (("EXTRACT_PROFESSORS",), ("save_professors",))
    if normalized == "crawler-loop-detection":
        return (
            ("DISCOVER_ORG_UNIT_PAGES", "EXTRACT_ORG_UNITS", "FIND_FACULTY_PAGES", "EXTRACT_PROFESSORS"),
            (),
        )
    return (("*",), ())


def _skill_applies_to_state(spec: SkillSpec, state: str) -> bool:
    if not spec.applies_to or "*" in spec.applies_to:
        return True
    return state in spec.applies_to


def _apply_token_budget(body: str, token_budget: int | None) -> str:
    if not token_budget or token_budget <= 0:
        return body
    # Approximate token-to-character ratio is enough for local skill snippets and
    # avoids coupling SkillManager to a specific tokenizer/model.
    char_budget = max(128, int(token_budget) * 4)
    if len(body) <= char_budget:
        return body
    return body[:char_budget].rstrip() + "\n[truncated by skill token_budget]"
