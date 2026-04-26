from __future__ import annotations

from pathlib import Path


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"
