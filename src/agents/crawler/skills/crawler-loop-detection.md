---
name: crawler-loop-detection
description: Detect and break out of crawler loops by analyzing backtrack count and visited URL patterns.
version: 2
applies_to: DISCOVER_ORG_UNIT_PAGES,EXTRACT_ORG_UNITS,FIND_FACULTY_PAGES,EXTRACT_PROFESSORS
allowed_tools:
priority: 80
token_budget: 700
created_at: 2026-04-26T12:49:52+00:00
updated_at: 2026-04-27T00:00:00+00:00
---
# Crawler Loop Detection & Recovery

## Loop Detection

When the same state sequence repeats and backtrack count increases, the crawler is likely in a loop.

### Loop Indicators

1. Backtrack count increases without new data being extracted
2. Same URLs being visited repeatedly across cycles
3. State transitions repeat: `DISCOVER_ORG_UNIT_PAGES -> EXTRACT_ORG_UNITS -> FIND_FACULTY_PAGES -> backtrack`
4. No professor data saved across multiple cycles

### Recovery Actions

When a loop is detected (3+ consecutive backtracks):

Action 1: Skip the current navigation path entirely.
- If the homepage links do not lead to org units or faculty pages, stop trying them
- Do not revisit the same URLs expecting different results
- The homepage may be JavaScript-rendered or simply not contain useful links

Action 2: Prefer official org unit listing pages.
- Look for pages like `院系设置`, `组织机构`, `学院设置`, `教学单位`, `科研机构`

Action 3: Try known faculty page paths on org unit subdomains.
- `/szdw/` (faculty team)
- `/teachers/`
- `/faculty/`
- `/people/`

Action 4: If all else fails, try searching.
- Use `site:university.edu.cn 师资队伍` / `教师名录` to find faculty pages on the right domain

## Critical Rule

Do not visit the same page more than once unless:
1. You have a new search query or a concrete new hypothesis, or
2. You intentionally disabled cross-run dedup during backtrack retries.
