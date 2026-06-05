---
name: org-unit-filter
description: Filter out non-target org units before faculty discovery.
version: 1
applies_to: EXTRACT_ORG_UNITS
allowed_tools:
priority: 15
token_budget: 1100
created_at: 2026-06-03T00:00:00
updated_at: 2026-06-03T00:00:00
---
## Goal

Keep only org units that should continue to faculty-page discovery. This is a blacklist task, not a broad academic whitelist.

## Exclude Categories

Exclude org units that clearly belong to any of these categories:

- Arts: 艺术学院, 美术学院, 音乐学院, 舞蹈学院, 戏剧学院, 戏曲学院, 电影学院, and equivalent arts/music/dance/drama/film schools.
- Sports: 体育学院, 体育系, 体育部, 体育教学部, 运动训练, physical education, sports, kinesiology.
- Sino-foreign or joint programs: 中外合作, 中外合办, 合作办学, 国际联合学院, 联合学院, joint institute, joint college, Sino-foreign programs.
- Basic teaching units: 基教中心, 基础教学中心, 基础教学部, 基础课教学部, 公共基础教学部.
- Teaching or experiment centers: 教学中心, 实验中心, 教学实验中心, 实验教学中心, 实训中心, 实践教学中心. These units do not recruit graduate students and must be excluded.
- Continuing/adult education: 继续教育学院, 成人教育学院, 成人高等教育, 网络教育学院, 开放教育学院, continuing education, adult education, online education.
- Undergraduate teaching units with no independent faculty roster: 书院, 本科生院, 本科生学院, 荣誉学院, 通识教育学院, 新生学院, 北航学院, residential college, undergraduate college, honors college.
- Person-named teaching colleges: 人名命名的学院/书院 only when page context shows they are undergraduate, honor, residential, general-education, or student-management groupings and do not own a distinct faculty roster.
- Excellent engineer teaching programs: 卓工, 卓越工程师学院, 卓越工程师培养, excellent engineer programs.
- Department sections under a college: 普通 `系` is not an independent org unit for recommendation. If it clearly belongs to a college/school, treat it as a faculty-list section under the parent college rather than as a separate included org unit.

## Sino-Foreign Examples

Treat these as excluded Sino-foreign or joint-program org units when they appear as colleges/schools:

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

## Keep Rules

- Keep 文, 理, 工, 医, 农, 商 org units unless they clearly match an exclude category above.
- Keep ambiguous org units when there is not enough evidence to exclude them.
- Keep person-named colleges when there is no clear evidence that they are undergraduate/honor/residential teaching units.
- Do not mistake 人工智能学院 for an arts org unit.
- Do not exclude 外国语学院 merely because it is not in 文理工医农商.
- Do not exclude an org unit only because its name contains 国际. Exclude 国际学院 only when it is clearly about Sino-foreign cooperation, international joint programs, joint training, 留学生教育, or international education rather than a normal academic school.
- Do not exclude 教育学院 or 高等教育研究院 unless they clearly mean continuing/adult/online/open education.
- Do not exclude research units merely because they contain 中心 or 实验. Keep 研究中心, 工程研究中心, 国家重点实验室, and other research labs/centers unless they clearly match teaching/experiment/training-center wording above.

## Output Format

When the input payload has `filter_task=org_unit_exclusion`, return JSON only:

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

Do not call tools in this task.
