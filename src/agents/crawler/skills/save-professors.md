---
name: save-professors
description: Extract public professor records and save them with strict field normalization.
version: 4
created_at: 2026-04-26T00:00:00
updated_at: 2026-04-28T20:10:00
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

- `name`: required; no extra punctuation or numbering.
- `title`: use only academic rank / role. Do not include honors.
- `enrollment_pref`: put advisor information here (博导/硕导), not in `title`.
- `email` / `phone` / `homepage` / `bio` / `research_areas` / `publications`: only if visible on the page.
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

- If a page is only a navigation page, continue to list/profile pages before saving.
- Prefer complete list extraction for each org unit page before moving on.
- Do not fabricate any value not present in page text.
