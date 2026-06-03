---
name: save-professors
description: Extract public professor records and save them with strict field normalization.
version: 5
applies_to: EXTRACT_PROFESSORS
allowed_tools: save_professors
priority: 20
token_budget: 1000
created_at: 2026-04-26T00:00:00
updated_at: 2026-06-03T00:00:00
---
## Goal

Extract public teacher records from official university pages and save high-quality structured data.

## Required Tool Call

Use `save_professors` with:

- `org_unit_name`: required, must match current page context.
- `org_unit_url`: optional but preferred.
- `source_url`: current page URL.
- `professors`: list of records.

## Field Rules (Strict)

- `name`: required; use the teacher's real name only. Remove extra punctuation, numbering,
  and low-value role markers such as `（兼）`, `(兼)`, or `兼职`.
- `title`: use only academic rank / role. Do not include honors.
- `enrollment_pref`: put advisor information here (博导/硕导), not in `title`.
- `email` / `phone` / `homepage` / `external_link` / `bio` / `research_areas` / `publications`: only if visible on the page.
- `homepage`: prefer the teacher's official profile/detail page. If the current `source_url`
  is a single-teacher profile page, it may be used as that teacher's `homepage`.
- Do not use roster/list pages, org-unit homepages, or faculty directory pages as a teacher `homepage`.
- Put external personal sites such as Google Scholar, ORCID, ResearchGate, or personal domains
  in `external_link`, not in `homepage`.
- Missing optional values should be `null` (not empty string `""`).

## Allowed Title Set

Prefer these normalized titles:

- `教授`
- `副教授`
- `研究员`
- `副研究员`
- `助理研究员`
- `讲师`
- `助理教授`
- `工程师`
- `院士` (for academicians)

## What Must Not Go Into `title`

- Awards and talent labels: such as 国家级教学名师, 杰青, 优青, 长江学者, 千人计划.
- Organization roles: 院长, 系主任, 党委书记 (unless no academic rank is available).
- Mixed bilingual text like `教授 (Professor)` or `Professor / National-level Teaching Master`.

## Precision and Coverage

- On official roster/list pages, save visible teacher names and academic titles even when contact or research fields are not shown; profile/detail pages can enrich those records later.
- If a page is only a navigation page with no teacher names, continue to list/profile pages before saving.
- Prefer complete list extraction for each org unit page before moving on.
- Do not fabricate any value not present in page text.

## Exclusion Strategy (Important)

- If the page is mainly news/notice/policy/recruitment/personnel content, do not call `save_professors`.
- Typical noise examples: `通知`, `公告`, `新闻`, `政策`, `规章`, `人事`, `招聘`, `党建`, `学工`, `招生`.
- Even under `szdw/jsdw/faculty` path, skip such non-roster pages and continue to actual teacher list/profile pages.
