---
name: crawler-loop-detection
description: Detect and break out of crawler loops by analyzing backtrack count and visited URL patterns.
version: 1
created_at: 2026-04-26T12:49:52+00:00
updated_at: 2026-04-26T12:49:52+00:00
---
# Crawler Loop Detection & Recovery

## Loop Detection

When the same state sequence repeats and backtrack count increases, the crawler is likely in a loop.

### Loop Indicators:
1. **Backtrack count increases** without new data being extracted
2. **Same URLs being visited** repeatedly across cycles
3. **State transitions**: FIND_COLLEGES → FIND_FACULTY_PAGES → backtrack → same states
4. **No professor data saved** across multiple cycles

### Recovery Actions:

When a loop is detected (3+ consecutive backtracks):

**Action 1: Skip the current navigation path entirely.**
- If the homepage links don't lead to faculty, STOP trying them
- Do NOT revisit the same URLs expecting different results
- The homepage may be JavaScript-rendered or simply not contain faculty links

**Action 2: Try direct subdomain URLs.**
- Most Chinese universities use subdomains for colleges: `[dept].university.edu.cn`
- Common subdomain prefixes: `www`, `econ`, `law`, `cs`, `math`, `physics`, `chem`, `sociology`, etc.
- Try `www.[dept].university.edu.cn` patterns

**Action 3: Try known faculty page paths.**
- `/szdw/` - 师资队伍 (most common)
- `/szdw/jsxx/` - 师资队伍/教师信息
- `/teachers/`
- `/faculty/`
- `/professor/`
- `/xygk/szdw/`
- `/ds/` - 导师

**Action 4: Try the about/overview page.**
- `/xxgk/` - 学校概况
- `/xygk/` - 学院概况
- `/yxsz/` - 院系设置

**Action 5: If all else fails, try searching.**
- Use site:university.edu.cn 师资队伍 in the crawler's search capability
- Or try `/szdw/index.html`, `/szdw/index.htm` variations

## Critical Rule

**Do NOT visit the same page more than once** unless:
1. Content is known to be dynamic/JavaScript-rendered
2. You have a new search query or parameter
3. A significant amount of time has passed

If you've visited `homepage_url/page1` and it didn't contain faculty links in the previous cycle, it will not contain them now. Skip it.