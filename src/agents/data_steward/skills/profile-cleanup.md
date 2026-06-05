---
name: profile-cleanup
description: 基于可信画像证据清理并补全教师字段。
version: 1
applies_to: STEWARD_PROFILE_CLEANUP
allowed_tools:
priority: 20
token_budget: 1000
created_at: 2026-06-05T00:00:00
updated_at: 2026-06-05T00:00:00
---
## 任务

对每个条目判断可见证据是否支持补全缺失画像字段。

返回格式：

```json
{"items":[{"entity_key":"professor:1","updates":{"bio":"...","research_areas":"...","title":"...","enrollment_pref":"..."},"reason":"profile_snapshot_cleanup","confidence":0.0,"evidence_spans":{"bio":["..."],"research_areas":["..."],"title":["..."],"enrollment_pref":["..."]},"recrawl_needed":false}]}
```

## 字段规则

- `bio`：使用真实可见的个人简介/画像段落，尤其是 `个人简介`、`简介`、`个人概况` 等标题后的正文。
- `research_areas`：使用简洁、可见的研究方向短语，包括 `研究方向` 或 `研究领域` 下的链接文本。
- `title`：只使用学术职称，例如 `教授`、`副教授`、`研究员`、`副研究员`、`助理研究员`、`讲师`、`助理教授`、`工程师`。
- `enrollment_pref`：`博士生导师`、`硕士生导师`、`博导`、`硕导` 放在这里，不要放入 `title`。
- 如果字段已经有值，不要重写，除非当前值明显为空、占位或无意义。

## 安全规则

- 不要把合作者、导师、团队、实验室或新闻作者相关文本当成当前人的字段。
- 不要复制用户提供但未出现在证据中的文本。
