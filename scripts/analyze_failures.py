"""Analyze failure patterns and diagnose why professors aren't being saved."""
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "yanclaw.db"


def main() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    print("=" * 70)
    print("1. HTTP vs HTTPS IN FAILED URLs")
    print("=" * 70)
    schemes = Counter()
    for row in conn.execute("SELECT url FROM crawl_logs WHERE status = 'failed'"):
        schemes[urlparse(row["url"]).scheme] += 1
    for scheme, count in schemes.most_common():
        print(f"  {scheme}: {count}")

    print()
    print("=" * 70)
    print("2. DOMAINS WITH MOST FAILURES")
    print("=" * 70)
    domains = Counter()
    for row in conn.execute("SELECT url FROM crawl_logs WHERE status = 'failed'"):
        domains[urlparse(row["url"]).hostname] += 1
    for domain, count in domains.most_common(15):
        print(f"  {domain:40s} {count}")

    print()
    print("=" * 70)
    print("3. UNIVERSITIES: SUCCESSFUL FETCHES vs PROFESSORS SAVED")
    print("=" * 70)
    for row in conn.execute("""
        SELECT u.name,
               SUM(CASE WHEN cl.status = 'success' THEN 1 ELSE 0 END) as success_count,
               SUM(CASE WHEN cl.status = 'failed' THEN 1 ELSE 0 END) as fail_count,
               (SELECT COUNT(*) FROM professors p
                JOIN colleges c ON c.id = p.college_id
                WHERE c.university_id = u.id) as prof_count
        FROM universities u
        LEFT JOIN crawl_logs cl ON cl.university_id = u.id
        GROUP BY u.id
        ORDER BY success_count DESC
    """):
        print(f"  {row['name']:20s} fetched={row['success_count']:3d} failed={row['fail_count']:3d} profs={row['prof_count']}")

    print()
    print("=" * 70)
    print("4. SUCCESSFULLY FETCHED URLs FOR ZERO-PROFESSOR UNIVERSITIES (sample)")
    print("=" * 70)
    for row in conn.execute("""
        SELECT u.name, cl.url
        FROM crawl_logs cl
        JOIN universities u ON u.id = cl.university_id
        WHERE cl.status = 'success'
        AND (SELECT COUNT(*) FROM professors p
             JOIN colleges c ON c.id = p.college_id
             WHERE c.university_id = u.id) = 0
        ORDER BY u.name, cl.created_at
        LIMIT 40
    """):
        print(f"  [{row['name']}] {row['url']}")

    print()
    print("=" * 70)
    print("5. BING SEARCH FALLBACK IN CRAWL LOGS")
    print("=" * 70)
    rows = conn.execute(
        "SELECT url, status FROM crawl_logs WHERE url LIKE '%bing.com%' ORDER BY created_at"
    ).fetchall()
    if rows:
        for row in rows:
            print(f"  [{row['status']}] {row['url'][:100]}")
    else:
        print("  (no Bing searches recorded in crawl_logs - search fallback bypasses crawl logging)")

    conn.close()


if __name__ == "__main__":
    main()
