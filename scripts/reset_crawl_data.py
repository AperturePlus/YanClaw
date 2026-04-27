"""Reset local crawl databases.

This deletes:
- data/yanclaw.db (legacy single-db storage, if present)
- data/universities/*.db (per-university crawl DBs)

Safety: requires --yes.
"""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete Yanclaw crawl DB files.")
    parser.add_argument(
        "--university-db-dir",
        default=str(Path("data") / "universities"),
        help="Directory containing per-university DBs (default: data/universities)",
    )
    parser.add_argument(
        "--legacy-db",
        default=str(Path("data") / "yanclaw.db"),
        help="Legacy DB path to delete if present (default: data/yanclaw.db)",
    )
    parser.add_argument("--yes", action="store_true", help="Actually delete files.")
    args = parser.parse_args(argv)

    uni_dir = Path(args.university_db_dir)
    legacy_db = Path(args.legacy_db)

    targets: list[Path] = []
    if legacy_db.exists():
        targets.append(legacy_db)
    if uni_dir.exists():
        targets.extend(sorted(uni_dir.glob("*.db")))

    if not targets:
        print("No DB files found.")
        return 0

    print("Will delete:")
    for path in targets:
        print(f"  {path}")

    if not args.yes:
        print("\nDry-run only. Re-run with --yes to delete.")
        return 2

    for path in targets:
        path.unlink(missing_ok=True)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

