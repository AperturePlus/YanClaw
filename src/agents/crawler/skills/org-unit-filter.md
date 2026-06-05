---
name: org-unit-filter
description: 在师资页发现前过滤非目标学院/机构。
version: 1
applies_to: EXTRACT_ORG_UNITS
allowed_tools:
priority: 15
token_budget: 1100
created_at: 2026-06-03T00:00:00
updated_at: 2026-06-03T00:00:00
---
## Goal

只保留应该继续进入师资页发现阶段的学院/机构。这是黑名单过滤任务，不是宽泛的学科白名单。

## 排除类别

明确属于以下类别的机构必须排除：

- 艺术类：艺术学院、美术学院、音乐学院、舞蹈学院、戏剧学院、戏曲学院、电影学院，以及等价的艺术、音乐、舞蹈、戏剧、电影类学院。
- 体育类：体育学院、体育系、体育部、体育教学部、运动训练、physical education、sports、kinesiology。
- 中外合作或联合项目：中外合作、中外合办、合作办学、国际联合学院、联合学院、joint institute、joint college、Sino-foreign programs。
- 基础教学单位：基教中心、基础教学中心、基础教学部、基础课教学部、公共基础教学部。
- 教学或实验中心：教学中心、实验中心、教学实验中心、实验教学中心、实训中心、实践教学中心。这类单位通常不是研究生招生学院，必须排除。
- 继续/成人教育：继续教育学院、成人教育学院、成人高等教育、网络教育学院、开放教育学院、continuing education、adult education、online education。
- 无独立教师名录的本科教学单位：书院、本科生院、本科生学院、荣誉学院、通识教育学院、新生学院、北航学院、residential college、undergraduate college、honors college。
- 人名命名教学学院/书院：只有当页面上下文表明它是本科、荣誉、住宿、通识教育或学生管理单位，且没有独立师资名录时才排除。
- 卓越工程师教学项目：卓工、卓越工程师学院、卓越工程师培养、excellent engineer programs。
- 学院下属系：普通 `系` 通常不是推荐系统中的独立学院。如果它明确隶属于某个学院，应作为父学院下的师资列表分区，而不是独立保留的 org unit。

## 中外合作示例

当以下名称作为学院/学校出现时，按中外合作或联合项目排除：

- 匹兹堡学院
- 格拉斯哥学院
- 巴黎卓越工程师学院
- 中法工程师学院
- 中德工程学院
- 中英国际学院
- 中美联合学院
- 密西根学院
- 爱丁堡学院
- 莱斯特国际学院

## 保留规则

- 文、理、工、医、农、商等普通学术单位默认保留，除非明确命中排除类别。
- 证据不足时保留，不要过度排除。
- 人名命名学院如果没有明确证据表明是本科/荣誉/住宿教学单位，应保留。
- 不要把人工智能学院误判为艺术类单位。
- 不要因为外国语学院不属于文理工医农商就排除。
- 不要仅因名称包含“国际”就排除。只有当国际学院明确指向中外合作、国际联合培养、留学生教育或国际教育，而不是普通学术学院时才排除。
- 不要排除教育学院或高等教育研究院，除非它们明确是继续、成人、网络或开放教育。
- 不要仅因名称包含“中心”或“实验”就排除研究单位。研究中心、工程研究中心、国家重点实验室等研究机构应保留，除非明确命中教学/实验/实训中心措辞。

## 输出格式

当输入包含 `filter_task=org_unit_exclusion` 时，只返回 JSON：

```json
{
  "included_org_units": [
    {"name": "...", "url": "...", "kind": "..."}
  ],
  "excluded_org_units": [
    {"name": "...", "url": "...", "reason": "arts|sports|joint_program|basic_teaching|teaching_experiment_center|continuing_education|undergraduate_teaching_unit|person_named_teaching_unit|excellent_engineer_program|sub_department_section"}
  ]
}
```

本任务不要调用任何工具。
