# 改进设计：中间页探索与关键词补全

## 1. 问题描述

以中国人民大学（RUC）为代表，部分高校的 org unit listing page 无法通过当前的
`_discover_org_unit_pages` 逻辑被正确发现。

### 1.1 RUC 实际网站结构

```
首页 www.ruc.edu.cn
├── 组织机构 → zuzhijigou.html          ← 包含所有教学机构及链接（目标页）
├── 师资队伍 → shiziduiwu.html
├── 研究生招生 → pgs.ruc.edu.cn
├── 教学机构 → zuzhijigou.html#1
└── ...（127 个同域链接）
```

`zuzhijigou.html` 是一个完美的 org unit listing page，列出了所有学院及其 URL。

### 1.2 当前逻辑失败原因

`_discover_org_unit_pages` 流程：

1. `_keyword_filter(home.links, ORG_UNIT_PAGE_KEYWORDS)` 对 URL 做关键词匹配
2. 匹配到 7 个链接（`xxgk`, `xiaoyuandaolan` 等），**但不包含 `zuzhijigou.html`**
3. 因为 `links` 非空，**跳过 LLM fallback**
4. 这 7 个链接全是误匹配，`_extract_org_units` 从中提取不到任何 org unit

**根因 A：关键词覆盖不足**

`ORG_UNIT_PAGE_KEYWORDS` 包含拼音缩写 `/jgsz`、`/zzjg`，但缺少全拼形式。
RUC 使用 `zuzhijigou`（组织机构全拼），不匹配任何现有关键词。

类似缺失的全拼：`jiaoxuejigou`（教学机构）、`shiziduiwu`（师资队伍）、
`yanjiusheng`（研究生）等。

**根因 B：单字关键词误匹配**

`院` 和 `系` 作为单字关键词，会匹配到大量无关 URL：
- `xiaoyuandaolan`（校园导览）包含 `yuan` → 匹配 `院`
- `xxgk`（信息公开）→ 匹配 `/xxgk`

误匹配导致 `links` 非空，跳过了本应触发的 LLM fallback。

## 2. 改进方案

### 2.1 补全拼音全拼关键词

在 `ORG_UNIT_PAGE_KEYWORDS` 中添加常见的拼音全拼形式：

```python
# 新增全拼
"zuzhijigou",    # 组织机构
"jiaoxuejigou",  # 教学机构
"jiaoxuedanwei", # 教学单位
"yuanxishezhi",  # 院系设置
"xueyuanshezhi", # 学院设置
"jigou",         # 机构（通用）
```

### 2.2 移除高误匹配率的单字关键词

从 `ORG_UNIT_PAGE_KEYWORDS` 中移除 `院` 和 `系`，保留更精确的多字词：

```python
# 移除：
"院",   # 误匹配率极高（校园、学院、医院...）
"系",   # 误匹配率极高（关系、联系、体系...）

# 保留：
"学院", "院系", "组织机构", "机构设置", "院系设置", "学院设置", "教学单位", "科研机构"
```

### 2.3 添加中间页探索（Intermediate Page Probing）

当 `_keyword_filter` 和 LLM fallback 都找不到 org unit pages 时，
在 search engine fallback 之前，增加一步：**主动探测常见中间页路径**。

这些中间页本身不是 org unit listing，但通常包含指向 org unit listing 的链接：

```python
_INTERMEDIATE_PAGE_PATHS = (
    "/zuzhijigou.html",   # 组织机构（RUC 等）
    "/jgsz.htm",          # 机构设置
    "/jgsz/",
    "/yxsz.htm",          # 院系设置
    "/yxsz/",
    "/zzjg.htm",          # 组织机构
    "/zzjg/",
    "/jxjg.htm",          # 教学机构
    "/jxjg/",
    "/xygk.htm",          # 学院概况
    "/xygk/",
)
```

探测逻辑：对 start_url 的 hostname 拼接这些路径，尝试 fetch，
成功的页面作为 org unit page candidates 返回。

### 2.4 不改动的部分

- 状态机流程不变（DISCOVER → EXTRACT → FIND_FACULTY → EXTRACT_PROFESSORS）
- max_depth / backtrack 机制不变
- LLM fallback 逻辑不变（仍在 keyword_filter 返回空时触发）
- search engine fallback 不变（仍作为最后手段）

## 3. 改动范围

| 文件 | 改动 |
|------|------|
| `agent.py` | `ORG_UNIT_PAGE_KEYWORDS` 补全拼音全拼，移除 `院`/`系` |
| `agent.py` | `_discover_org_unit_pages` 中添加中间页探测步骤 |
| `agent.py` | 新增 `_INTERMEDIATE_PAGE_PATHS` 常量和 `_probe_intermediate_pages` 方法 |
| `test_agent.py` | 添加中间页探测的单元测试 |

## 4. 预期效果

对 RUC：
1. `zuzhijigou` 全拼关键词直接匹配 `zuzhijigou.html` ✓
2. 即使关键词不匹配，中间页探测也会找到 `/zuzhijigou.html` ✓
3. 移除 `院`/`系` 后不再误匹配 `xiaoyuandaolan` 等无关链接 ✓

对其他大学：
- 使用 `/jgsz`、`/zzjg` 等缩写路径的大学不受影响（关键词保留）
- 使用全拼路径的大学受益于新增关键词
- 中间页探测作为额外保险层，不影响已有逻辑的正常路径
