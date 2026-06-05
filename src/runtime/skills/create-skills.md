---
name: create-skills
description: 创建和维护 Markdown 技能文件的规则。
version: 1
applies_to: "*"
allowed_tools:
priority: 100
token_budget: 700
created_at: 2026-04-26T00:00:00
updated_at: 2026-04-26T00:00:00
---

## Goal
创建和维护可长期复用的 Markdown 技能说明，让后续智能体在相同场景下能稳定采用正确规则。

## Rules

- 技能文件必须以 frontmatter 开头，至少保留 `name`、`description`、`version`、`created_at`、`updated_at`。
- 运行时选择技能依赖 `applies_to`、`allowed_tools`、`priority`、`token_budget`；修改时不要随意删除这些字段。
- 更新技能时直接编辑当前 Markdown 文件即可，不再记录数据库历史版本。
- 技能内容应聚焦具体、可执行的决策规则，避免写成宽泛原则。
- 只修改与当前需求相关的说明，不要覆盖无关规则。

## Usage

- 只有当现有技能无法覆盖重复出现的模式时，才创建新技能。
- 如果已有技能范围正确，只需要补充规则、示例或边界情况，就编辑该技能文件。
- 新技能应保持短小，明确说明适用状态、允许工具、输出格式和不能做的事。
