"""Analyze the Yanclaw crawler SQLite database."""
import sqlite3
import sys
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "yanclaw.db"


def main() -> None:
    if not DB_PATH.exists():
        print(f"Database not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    print("=" * 70)
    print("1. UNIVERSITY STATUS SUMMARY")
    print("=" * 70)
    for row in conn.execute(
        "SELECT crawl_status, COUNT(*) as cnt FROM universities GROUP BY crawl_status ORDER BY cnt DESC"
    ):
        print(f"  {row['crawl_status']:15s} {row['cnt']}")

    print()
    print("=" * 70)
    print("2. UNIVERSITIES WITH PROFESSORS")
    print("=" * 70)
    for row in conn.execute("""
        SELECT u.name, u.crawl_status, COUNT(DISTINCT p.id) as prof_count
        FROM universities u
        LEFT JOIN colleges c ON c.university_id = u.id
        LEFT JOIN professors p ON p.college_id = c.id
        GROUP BY u.id
        ORDER BY prof_count DESC
    """):
        marker = " *" if row["prof_count"] > 0 else ""
        print(f"  {row['name']:20s} status={row['crawl_status']:12s} professors={row['prof_count']}{marker}")

    print()
    print("=" * 70)
    print("3. CRAWL LOG SUMMARY (success vs failed)")
    print("=" * 70)
    for row in conn.execute(
        "SELECT status, COUNT(*) as cnt FROM crawl_logs GROUP BY status ORDER BY cnt DESC"
    ):
        print(f"  {row['status']:10s} {row['cnt']}")

    print()
    print("=" * 70)
    print("4. FAILED FETCHES BY UNIVERSITY (top 10)")
    print("=" * 70)
    for row in conn.execute("""
        SELECT u.name, COUNT(*) as fail_count
        FROM crawl_logs cl
        JOIN universities u ON u.id = cl.university_id
        WHERE cl.status = 'failed'
        GROUP BY u.name
        ORDER BY fail_count DESC
        LIMIT 10
    """):
        print(f"  {row['name']:20s} {row['fail_count']} failed fetches")

    print()
    print("=" * 70)
    print("5. FAILED URLs SAMPLE (last 20)")
    print("=" * 70)
    for row in conn.execute("""
        SELECT u.name, cl.url, cl.message
        FROM crawl_logs cl
        JOIN universities u ON u.id = cl.university_id
        WHERE cl.status = 'failed'
        ORDER BY cl.created_at DESC
        LIMIT 20
    """):
        print(f"  [{row['name']}] {row['url']}")
        if row["message"]:
            print(f"    -> {row['message'][:100]}")

    print()
    print("=" * 70)
    print("6. PROFESSORS SAMPLE (first 10)")
    print("=" * 70)
    for row in conn.execute("""
        SELECT p.name, p.title, p.email, p.research_areas, c.name as college, u.name as university
        FROM professors p
        JOIN colleges c ON c.id = p.college_id
        JOIN universities u ON u.id = c.university_id
        LIMIT 10
    """):
        print(f"  {row['university']} / {row['college']} / {row['name']}")
        if row["title"]:
            print(f"    title: {row['title']}")
        if row["email"]:
            print(f"    email: {row['email']}")
        if row["research_areas"]:
            print(f"    research: {str(row['research_areas'])[:80]}")

    print()
    print("=" * 70)
    print("7. COLLEGES DISCOVERED")
    print("=" * 70)
    for row in conn.execute("""
        SELECT u.name as university, COUNT(c.id) as college_count
        FROM universities u
        LEFT JOIN colleges c ON c.university_id = u.id
        GROUP BY u.id
        HAVING college_count > 0
        ORDER BY college_count DESC
    """):
        print(f"  {row['university']:20s} {row['college_count']} colleges")

    print()
    print("=" * 70)
    print("8. SKILL VERSIONS")
    print("=" * 70)
    for row in conn.execute(
        "SELECT skill_name, version, change_summary, created_at FROM skill_versions ORDER BY created_at"
    ):
        print(f"  {row['skill_name']} v{row['version']} ({row['created_at']}): {row['change_summary'][:60]}")

    conn.close()


if __name__ == "__main__":
    main()
