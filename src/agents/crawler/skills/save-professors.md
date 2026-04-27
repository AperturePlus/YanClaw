---
name: save-professors
description: Extract public professor information from faculty pages and save it with the save_professors tool.
version: 3
created_at: 2026-04-26T00:00:00
updated_at: 2026-04-26T18:58:00
---
## Goal

Identify professor records that are visible in public university pages and save them in structured form.

## Tool Definition

Use `save_professors` with `org_unit_name` and `professors`.

Each professor must include `name`. Optional fields include `title`, `research_areas`, `email`, `phone`, `homepage`, `bio`, `enrollment_pref`, and `publications`.

## Guidance

- Save only information present in the page text.
- Prefer professors discovered through the official college/school site, then its faculty/team/teacher pages.
- Prefer the teacher's profile URL for `homepage` when available.
- Use empty optional fields rather than inventing values.
- Batch records from the same org unit in one tool call.
- If the same teacher appears in multiple org units, keep stable identity fields (`email` and `homepage`) unchanged so the database can merge the person and record multiple affiliations.

## Chinese Academic Title Handling

Map Chinese academic titles to English where useful:

- 教授 → Professor
- 副教授 → Associate Professor
- 助理教授 → Assistant Professor
- 讲师 → Lecturer
- 助教 → Teaching Assistant
- 研究员 → Researcher
- 副研究员 → Associate Researcher
- 助理研究员 → Assistant Researcher
- 教授级高工 → Professor-level Senior Engineer
- 高级工程师 → Senior Engineer
- 博士生导师 / 博导 → PhD Supervisor / Doctoral Advisor
- 硕士生导师 / 硕导 → Master's Supervisor
- 院士 → Academician
- 长江学者 → Changjiang Scholar
- 杰青 → Distinguished Young Scholar
- 优青 → Excellent Young Scholar
- 百人计划 → Hundred Talents Program
- 千人计划 → Thousand Talents Program

## Field mapping notes

- `title`: Position/title like "教授", "副教授", "Professor", "Associate Professor"
- `research_areas`: Can be a string or array of strings describing research interests/specialties
- `enrollment_pref`: Whether they accept graduate students (博导/硕导 info)
- `publications`: Can be a string or array of strings listing key publications
- `bio`: Full biography, introduction, or profile text
- `email`: Email address if visible on the page
- `phone`: Phone number if visible
- `homepage`: Personal/academic homepage URL, prefer the profile URL on the university site
