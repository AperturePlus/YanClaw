---
name: steward-evidence-rules
description: DataSteward LLM 清理任务共享的证据和 JSON 规则。
version: 1
applies_to: *
allowed_tools:
priority: 10
token_budget: 700
created_at: 2026-06-05T00:00:00
updated_at: 2026-06-05T00:00:00
---
## Goal

只使用提供的数据库字段和页面快照来提升爬虫数据库质量。

## 证据规则

- 不要编造事实、职称、研究方向、简介文本、隶属关系或院士身份。
- 每个拟更新字段或身份动作都必须包含 `evidence_spans`。
- 每条证据必须是输入的 `bio`、`profile_text` 或 `name_window` 中可见的精确子串。
- 如果证据薄弱、缺失、只是关系描述，或指向其他人，不要更新；有价值时请求补抓。
- 与其猜测，不如设置 `recrawl_needed=true`。

## 输出规则

- 只返回严格 JSON。
- 没有安全动作时使用空数组或空对象。
- `confidence` 必须在 `0.0` 到 `1.0` 范围内。
- 不要在 JSON 外输出 Markdown、注释或解释。
