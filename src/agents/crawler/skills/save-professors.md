---
name: save-professors
description: Extract public professor records and save them with strict field normalization.
version: 8
applies_to: EXTRACT_PROFESSORS
allowed_tools: save_professors
priority: 20
token_budget: 1000
created_at: 2026-04-26T00:00:00
updated_at: 2026-06-05
---
## Goal

Extract public teacher records from official university pages and save high-quality structured data.

## Required Tool Call

Use `save_professors` with:

- `org_unit_name`: required, must match current page context.
- `org_unit_url`: optional but preferred.
- `source_url`: current page URL.
- `professors`: list of records.

## Org Unit Context Rules

- Use the current task's parent college/school as `org_unit_name`. Do not replace it with a page heading such as a department, teaching center, experiment center, lab section, or roster category.
- If the current page is a department list under a college (for example `工业互联网与建模仿真系` under `自动化科学与电气工程学院`), extract visible teachers but still call `save_professors` with the parent college/school name.
- If the current page is a teaching/experiment/training center roster (`教学中心`, `实验中心`, `教学实验中心`, `实验教学中心`, `实训中心`, `实践教学中心`), do not call `save_professors`; these units are not graduate-admission org units.

## Field Rules (Strict)

- `name`: required; use the teacher's real name only. Remove extra punctuation, numbering,
  and low-value role markers such as `（兼）`, `(兼)`, or `兼职`.
- For foreign teachers shown as `Latin Name (中文名)` or `Latin Name（中文名）`, put only
  the Latin name in `name`; do not include the parenthesized Chinese alias.
- `title`: use only academic rank / role. Do not include honors.
- `enrollment_pref`: put advisor information here (博导/硕导), not in `title`.
- `email` / `phone` / `homepage` / `external_link` / `bio` / `research_areas` / `publications`: only if visible on the page.
- `is_academician`: set to `true` when the page explicitly says the person is an academician
  (`院士`, `中国科学院院士`, `中国工程院院士`, or `Academician`), even if their academic rank is also `教授`.
- Do not set `is_academician=true` just because the person's advisor, collaborator, team leader,
  lab, project, `院士工作站`, `院士团队`, or `院士课题组` mentions an academician. The academician
  identity must refer to this teacher personally.
- `homepage`: prefer the teacher's official profile/detail page. If the current `source_url`
  is a single-teacher profile page, it may be used as that teacher's `homepage`.
- Do not use roster/list pages, org-unit homepages, or faculty directory pages as a teacher `homepage`.
- Put external personal sites such as Google Scholar, ORCID, ResearchGate, or personal domains
  in `external_link`, not in `homepage`.
- Missing optional values should be `null` (not empty string `""`).
- If `bio` contains explicit research phrases such as `研究方向`, `研究领域`,
  `主要从事...研究`, or `在...方面取得...研究成果`, extract those phrases into
  `research_areas` as well; do not leave them only in `bio`.

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
