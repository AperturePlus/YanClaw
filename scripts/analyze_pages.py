"""Analyze what pages were actually fetched and whether they contain faculty data."""
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "yanclaw.db"


def classify_url(url: str) -> str:
    """Classify a URL by what kind of page it likely is."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path.lower()

    # Faculty platform (NOT real faculty lists)
    if host.startswith("faculty.") or host.startswith("faculty-"):
        return "faculty_platform"

    # University homepage
    if host.startswith("www.") and path in ("/", "/index.htm", "/index.html", ""):
        return "university_homepage"

    # College/school subdomain homepage
    if not host.startswith("www.") and not host.startswith("faculty") and path in ("/", "/index.htm", "/index.html", ""):
        return "college_subdomain_homepage"

    # Faculty/teacher list pages (the real ones)
    faculty_keywords = ("szdw", "szll", "teacher", "faculty", "szrc", "jszy", "szgk", "rydw")
    if any(kw in path for kw in faculty_keywords):
        return "likely_faculty_list"

    # College pages on main domain
    college_keywords = ("xy", "yxsz", "xbyx", "department", "school", "college", "yuan")
    if any(kw in path for kw in college_keywords):
        return "college_page"

    # Admin/info pages
    admin_keywords = ("xxgk", "zzjg", "jgsz", "jxgl", "about", "news")
    if any(kw in path for kw in admin_keywords):
        return "admin_page"

    # Individual teacher page
    if "/zh_CN/index.htm" in path or "/en/index.htm" in path:
        return "individual_teacher_page"

    # Pagination
    if "page" in path.lower() or "PAGENUM" in url:
        return "pagination"

    return "other"


def main() -> None:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    print("=" * 70)
    print("1. PAGE TYPE DISTRIBUTION (all successful fetches)")
    print("=" * 70)
    types = Counter()
    for row in conn.execute("SELECT url FROM crawl_logs WHERE status = 'success'"):
        types[classify_url(row["url"])] += 1
    for ptype, count in types.most_common():
        print(f"  {ptype:30s} {count:4d}")

    print()
    print("=" * 70)
    print("2. FACULTY PLATFORM PAGES (NOT real faculty lists)")
    print("=" * 70)
    print("  These are teacher homepage portals, not actual faculty lists:")
    for row in conn.execute("SELECT DISTINCT url FROM crawl_logs WHERE status = 'success' ORDER BY url"):
        if classify_url(row["url"]) == "faculty_platform":
            print(f"    {row['url']}")

    print()
    print("=" * 70)
    print("3. INDIVIDUAL TEACHER PAGES FETCHED (wasted effort)")
    print("=" * 70)
    teacher_pages = []
    for row in conn.execute("SELECT url FROM crawl_logs WHERE status = 'success'"):
        if classify_url(row["url"]) == "individual_teacher_page":
            teacher_pages.append(row["url"])
    print(f"  Total: {len(teacher_pages)} individual teacher pages fetched")
    for url in teacher_pages[:10]:
        print(f"    {url}")
    if len(teacher_pages) > 10:
        print(f"    ... and {len(teacher_pages) - 10} more")

    print()
    print("=" * 70)
    print("4. PAGINATION PAGES (potential infinite crawl)")
    print("=" * 70)
    pagination = []
    for row in conn.execute("SELECT url FROM crawl_logs WHERE status = 'success'"):
        if classify_url(row["url"]) == "pagination":
            pagination.append(row["url"])
    print(f"  Total: {len(pagination)} pagination pages fetched")
    for url in pagination[:10]:
        print(f"    {url}")
    if len(pagination) > 10:
        print(f"    ... and {len(pagination) - 10} more")

    print()
    print("=" * 70)
    print("5. REAL FACULTY LIST PAGES (likely_faculty_list)")
    print("=" * 70)
    for row in conn.execute("SELECT DISTINCT url FROM crawl_logs WHERE status = 'success' ORDER BY url"):
        if classify_url(row["url"]) == "likely_faculty_list":
            print(f"    {row['url']}")
    if not conn.execute(
        "SELECT 1 FROM crawl_logs WHERE status = 'success'"
    ).fetchone():
        print("  (none found)")

    print()
    print("=" * 70)
    print("6. COLLEGE SUBDOMAIN HOMEPAGES (should navigate to /szdw/ next)")
    print("=" * 70)
    for row in conn.execute("SELECT DISTINCT url FROM crawl_logs WHERE status = 'success' ORDER BY url"):
        if classify_url(row["url"]) == "college_subdomain_homepage":
            print(f"    {row['url']}")

    print()
    print("=" * 70)
    print("7. PER-UNIVERSITY PAGE TYPE BREAKDOWN (zero-professor universities)")
    print("=" * 70)
    for uni_row in conn.execute("""
        SELECT u.id, u.name FROM universities u
        WHERE (SELECT COUNT(*) FROM professors p
               JOIN colleges c ON c.id = p.college_id
               WHERE c.university_id = u.id) = 0
        AND (SELECT COUNT(*) FROM crawl_logs cl
             WHERE cl.university_id = u.id AND cl.status = 'success') > 5
        ORDER BY u.name
    """):
        types_for_uni = Counter()
        for row in conn.execute(
            "SELECT url FROM crawl_logs WHERE university_id = ? AND status = 'success'",
            (uni_row["id"],),
        ):
            types_for_uni[classify_url(row["url"])] += 1
        print(f"\n  {uni_row['name']}:")
        for ptype, count in types_for_uni.most_common():
            print(f"    {ptype:30s} {count}")

    conn.close()


if __name__ == "__main__":
    main()
