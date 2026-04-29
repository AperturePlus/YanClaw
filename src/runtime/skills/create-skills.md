---
name: create-skills
description: Guidance for creating and updating markdown skills with version history.
version: 1
created_at: 2026-04-26T00:00:00
updated_at: 2026-04-26T00:00:00
---

## Goal
Create and improve agent skills as durable markdown instructions.

## Rules

- A skill file must start with frontmatter containing `name`, `description`, `version`, `created_at`, and `updated_at`.
- Before updating a skill, call `update_skill`; the runtime stores the current file content in the database before writing the new version.
- Prefer narrow, operational guidance that helps the agent make better decisions on later runs.
- Do not overwrite unrelated guidance.

## Tool Usage

- Use `create_skill` only when no existing skill covers the repeated pattern.
- Use `update_skill` when a current skill is correct in scope but needs a better rule, example, or edge case.
