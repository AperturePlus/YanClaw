from __future__ import annotations

import difflib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from runtime.database import DatabaseManager, SkillVersion


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    version: int
    path: Path


@dataclass(frozen=True)
class VersionInfo:
    version: int
    change_summary: str
    agent_name: str
    created_at: datetime
    is_current: bool = False


class SkillManager:
    """Markdown skill loader with database-backed version history."""

    def __init__(self, skills_dir: Path, db: DatabaseManager, agent_name: str) -> None:
        self.skills_dir = Path(skills_dir)
        self.db = db
        self.agent_name = agent_name
        self.skills_dir.mkdir(parents=True, exist_ok=True)

    def list_skills(self) -> list[SkillMeta]:
        metas: list[SkillMeta] = []
        for path in sorted(self.skills_dir.glob("*.md")):
            content = path.read_text(encoding="utf-8")
            frontmatter, _ = parse_frontmatter(content)
            name = str(frontmatter.get("name") or path.stem)
            description = str(frontmatter.get("description") or "")
            version = int(frontmatter.get("version") or 1)
            metas.append(SkillMeta(name=name, description=description, version=version, path=path))
        return metas

    def load_skill(self, name: str) -> str:
        return self._skill_path(name).read_text(encoding="utf-8")

    def load_skills(self, names: list[str]) -> dict[str, str]:
        return {name: self.load_skill(name) for name in names}

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

    async def update_skill(self, name: str, new_content: str, change_summary: str) -> int:
        current = self.load_skill(name)
        current_meta, _ = parse_frontmatter(current)
        current_version = int(current_meta.get("version") or 1)
        await self._store_version(name, current_version, current, change_summary)

        new_version = current_version + 1
        normalized = self._with_frontmatter(
            name=name,
            content=new_content,
            description=str(current_meta.get("description") or ""),
            version=new_version,
            created_at=_parse_datetime(current_meta.get("created_at")) or datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self._skill_path(name).write_text(normalized, encoding="utf-8")
        return new_version

    async def rollback_skill(self, name: str, target_version: int) -> None:
        current = self.load_skill(name)
        current_meta, _ = parse_frontmatter(current)
        current_version = int(current_meta.get("version") or 1)
        await self._store_version(
            name,
            current_version,
            current,
            f"Stored current version before rollback to v{target_version}",
        )
        target_content = await self._content_for_version(name, target_version)
        self._skill_path(name).write_text(target_content, encoding="utf-8")

    async def diff_skill(self, name: str, v1: int, v2: int) -> str:
        content_1 = await self._content_for_version(name, v1)
        content_2 = await self._content_for_version(name, v2)
        return "".join(
            difflib.unified_diff(
                content_1.splitlines(keepends=True),
                content_2.splitlines(keepends=True),
                fromfile=f"{name}@v{v1}",
                tofile=f"{name}@v{v2}",
            )
        )

    async def get_history(self, name: str) -> list[VersionInfo]:
        async with self.db.session() as session:
            rows = (
                await session.execute(
                    select(SkillVersion)
                    .where(
                        SkillVersion.agent_name == self.agent_name,
                        SkillVersion.skill_name == name,
                    )
                    .order_by(SkillVersion.version)
                )
            ).scalars().all()

        history = [
            VersionInfo(
                version=row.version,
                change_summary=row.change_summary,
                agent_name=row.agent_name,
                created_at=row.created_at,
                is_current=False,
            )
            for row in rows
        ]

        path = self._skill_path(name)
        if path.exists():
            frontmatter, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            current_version = int(frontmatter.get("version") or 1)
            if all(info.version != current_version for info in history):
                history.append(
                    VersionInfo(
                        version=current_version,
                        change_summary="current file",
                        agent_name=self.agent_name,
                        created_at=datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc),
                        is_current=True,
                    )
                )
            else:
                history = [
                    VersionInfo(
                        version=info.version,
                        change_summary=info.change_summary,
                        agent_name=info.agent_name,
                        created_at=info.created_at,
                        is_current=info.version == current_version,
                    )
                    for info in history
                ]
        return sorted(history, key=lambda item: item.version)

    async def _store_version(
        self,
        name: str,
        version: int,
        content: str,
        change_summary: str,
    ) -> None:
        async with self.db.session() as session:
            existing = (
                await session.execute(
                    select(SkillVersion).where(
                        SkillVersion.agent_name == self.agent_name,
                        SkillVersion.skill_name == name,
                        SkillVersion.version == version,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                existing.content = content
                existing.change_summary = change_summary
                existing.created_at = datetime.now(timezone.utc)
            else:
                session.add(
                    SkillVersion(
                        skill_name=name,
                        version=version,
                        content=content,
                        change_summary=change_summary,
                        agent_name=self.agent_name,
                    )
                )

    async def _content_for_version(self, name: str, version: int) -> str:
        path = self._skill_path(name)
        if path.exists():
            current = path.read_text(encoding="utf-8")
            frontmatter, _ = parse_frontmatter(current)
            if int(frontmatter.get("version") or 1) == version:
                return current

        async with self.db.session() as session:
            row = (
                await session.execute(
                    select(SkillVersion).where(
                        SkillVersion.agent_name == self.agent_name,
                        SkillVersion.skill_name == name,
                        SkillVersion.version == version,
                    )
                )
            ).scalar_one_or_none()
        if not row:
            raise KeyError(f"Version not found: {name}@v{version}")
        return row.content

    def _skill_path(self, name: str) -> Path:
        if "/" in name or "\\" in name or name in {"", ".", ".."}:
            raise ValueError(f"Invalid skill name: {name!r}")
        return self.skills_dir / f"{name}.md"

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


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
