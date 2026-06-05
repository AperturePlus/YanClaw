---
name: crawler-loop-detection
description: 通过回退次数和已访问 URL 模式识别并打破爬虫循环。
version: 2
applies_to: DISCOVER_ORG_UNIT_PAGES,EXTRACT_ORG_UNITS,FIND_FACULTY_PAGES,EXTRACT_PROFESSORS
allowed_tools:
priority: 80
token_budget: 700
created_at: 2026-04-26T12:49:52+00:00
updated_at: 2026-04-27T00:00:00+00:00
---
# 爬虫循环识别与恢复

## 循环判断

如果同一组状态反复出现，并且回退次数持续增加，说明当前导航路径很可能已经陷入循环。

### 典型信号

1. 回退次数增加，但没有提取到新的学院、师资页或教师数据。
2. 多轮循环中反复访问相同 URL。
3. 状态序列重复，例如 `DISCOVER_ORG_UNIT_PAGES -> EXTRACT_ORG_UNITS -> FIND_FACULTY_PAGES -> backtrack`。
4. 连续多轮没有保存任何教师记录。

### 恢复动作

当检测到循环，尤其是连续 3 次以上回退时，按以下优先级处理：

动作 1：彻底跳过当前导航路径。
- 如果主页链接没有导向学院列表或师资页，就停止继续尝试这条路径。
- 不要反复访问同一 URL 并期待不同结果。
- 主页可能依赖 JavaScript 渲染，或者本身不包含有用链接。

动作 2：优先寻找官方学院/机构列表页。
- 重点查找 `院系设置`、`组织机构`、`学院设置`、`教学单位`、`科研机构` 等页面。

动作 3：在学院子站上尝试常见师资路径。
- `/szdw/`
- `/teachers/`
- `/faculty/`
- `/people/`

动作 4：如果仍然没有候选，使用搜索。
- 用 `site:university.edu.cn 师资队伍` 或 `site:university.edu.cn 教师名录` 查找同域师资页。

## 关键规则

不要重复访问同一页面，除非满足以下条件之一：
1. 有新的搜索词或明确的新假设。
2. 回退重试时已经有意关闭跨运行去重。
