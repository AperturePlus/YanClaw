---
name: missing-field-triage
description: 判断字段缺失原因，并决定是否需要补抓。
version: 1
applies_to: STEWARD_MISSING_FIELD_TRIAGE
allowed_tools:
priority: 20
token_budget: 800
created_at: 2026-06-05T00:00:00
updated_at: 2026-06-05T00:00:00
---
## 任务

判断必需画像字段为什么缺失，并决定是否需要排入详情页补抓任务。

返回格式：

```json
{"items":[{"id":1,"reason":"homepage_profile_incomplete","confidence":0.0,"recrawl_needed":true,"evidence_spans":["..."]}]}
```

允许的 `reason` 值：

- `homepage_profile_incomplete`
- `crawl_failure`
- `site_missing`
- `uncertain`

## 规则

- 如果教师有 `homepage`，但 `bio` 或 `research_areas` 缺失，使用 `homepage_profile_incomplete`。
- 只有证据中明确包含爬虫失败时，才使用 `crawl_failure`。
- 如果官方页面可访问，但确实没有公开该字段，使用 `site_missing`。
- 如果证据不足，使用 `uncertain`。
