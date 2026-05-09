---
name: extract-links
description: Select same-site links that maximize org-unit and faculty coverage.
version: 7
created_at: 2026-04-26T00:00:00
updated_at: 2026-04-28T20:10:00
---
## Goal

Pick links that help the crawler cover as many colleges/schools and faculty directories as possible.

## Priority Order

1. University-level org-unit index pages.
2. College/school/department homepages.
3. Faculty/team/teacher list pages for each org unit.
4. Individual teacher profile pages.

## Org-Unit Discovery Hints

Prefer links containing:

- `jgsz`, `yxsz`, `xysz`, `zzjg`, `jxjg`
- `college`, `school`, `department`, `academy`
- `院系`, `学院`, `组织机构`, `机构设置`, `教学单位`

## Faculty Discovery Hints

Prefer links containing:

- `faculty`, `teacher`, `staff`, `people`, `team`, `directory`, `list`
- `szdw`, `szll`, `jsdw`, `qzjs`
- `师资`, `教师`, `导师`, `教工`

## Coverage Rules

- Keep same-site links only.
- For each org unit, try to keep at least one faculty-list candidate.
- If both showcase pages and directory pages exist, prefer directory pages.
- Do not prioritize academicians-only pages over full teacher directories.

## Exclusions

- News, notices, events, admissions, student affairs, alumni pages.
- HR/personnel/recruitment pages (e.g. `renshi`, `hr`, `rczp`, `zhaopin`, `jobs`).
- Policy/rules/party pages (e.g. `policy`, `zcwj`, `dangjian`, `party`).
- Login/SSO/mail/library/download pages.
- External non-university domains.

## Anti-Noise Rule (Important)

- If a page is under a faculty-related path but is actually a notice/news/policy list, treat it as noise and skip it.
- Prefer stable directory/profile patterns such as `szdw`, `jsdw`, `teacher`, `faculty`, `people`, `/info/...`.

## Tool Usage

Call `extract_links` with:

- `links`: raw candidate links from current page.
- `base_url`: current university root URL.
- `keywords`: optional, but keep broad enough to avoid empty results.
