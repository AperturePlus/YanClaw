---
name: org-unit-cleanup
description: 为 DataSteward 复核待清理的学院/机构候选。
version: 1
applies_to: STEWARD_ORG_UNIT_CLEANUP
allowed_tools:
priority: 20
token_budget: 900
created_at: 2026-06-05T00:00:00
updated_at: 2026-06-05T00:00:00
---
## 任务

过滤不应作为独立研究生招生学院/学校保留在爬虫数据库中的机构。

严格返回 JSON：

```json
{"included_org_units":[{"id":1,"name":"..."}],"excluded_org_units":[{"id":2,"name":"...","category":"teaching_experiment_center","reason":"...","keyword":"..."}]}
```

## 明确时排除

- 教学、实验、实训、本科住宿、成人/继续教育、艺术、体育、中外合作或联合项目单位。
- 没有独立师资的荣誉学院或人名命名本科教学组织。
- 应合并到父级学院下的系或分区，可归类为 `sub_department_section`。

## 不确定时保留

- 普通学院、学校、系、研究院、实验室，以及拥有研究生导师队伍的单位。
- 如果证据表明人名命名学院是真实学术学院且拥有师资，应保留。
