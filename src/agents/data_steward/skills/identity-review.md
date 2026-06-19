---
name: identity-review
description: 复核院士身份，避免因关系描述造成误判。
version: 1
applies_to: STEWARD_IDENTITY_REVIEW
allowed_tools:
priority: 20
token_budget: 1000
created_at: 2026-06-05T00:00:00
updated_at: 2026-06-05T00:00:00
---
## 任务

判断当前实体本人是否为院士、普通教师，或应保持不变。

返回格式：

```json
{"items":[{"entity_key":"professor:1","action":"promote_academician","updates":{"title":"院士","research_areas":"..."},"reason":"self_academician_evidence","confidence":0.0,"evidence_spans":["..."],"recrawl_needed":false}]}
```

允许的 `action` 值：

- `promote_academician`
- `demote_to_professor`
- `keep_professor`
- `keep_academician`
- `no_action`

## 身份规则

- `院士`、`中国科学院院士`、`中国工程院院士` 或 `Academician` 必须指向条目中的这个人本人。
- 不要因为导师、合作者、团队负责人、实验室、项目、`院士工作站`、`院士团队`、`院士课题组` 提到院士，就把当前人标为院士。
- 如果文本只说明此人是 `教授`、`副教授`、`研究员` 等，没有说明其本人是院士，只有当该实体当前存为院士时才使用 `demote_to_professor`。
- 不确定时使用 `no_action`，或保持当前实体类型。
