---
name: extract-links
description: Select same-domain links that are likely to lead to colleges, schools, faculty lists, or teacher profiles.
version: 6
created_at: 2026-04-26T00:00:00
updated_at: 2026-04-26T11:37:04
---
## Extract Links for University Faculty Crawling

Select same-domain links that are likely to lead to colleges, schools, faculty lists, or teacher profiles.

### Prioritization rules

**Required crawl order**:

- First find the official college/school/department site from the university site.
- Then search that college/school site for faculty/team/teacher pages.
- Only follow teacher profile pages after they are discovered from an official college/school faculty page.
- Do not jump from the university home page to random teacher pages unless the page clearly belongs to an official college/school site.

**For college/school discovery** (first hop):

- Prefer links containing: school, college, department, academy, faculty, 院, 系, 学院, 研究院, 研究所, 实验室
- URLs matching patterns: `/school/`, `/college/`, `/dept/`, `/department/`, `/xy/`, `/yx/`, `/dep/`, `/院系/`, `/学院/`
- Chinese portal patterns: `/xygk`, `/xxgk`, `/szdw`, `/yxsz`, `/组织机构`, `/院系设置`, `/院系概况`

**Additional patterns for Chinese university homepages** (many have hardcoded footer links):

- Look for links in the footer/底部 area containing: 院系, 学院, 系, 教学单位, 科研机构
- Common URL path segments: `/xy/`, `/yx/`, `/xygk/`, `/yxsz/`, `/jgsz/`, `/zzjg/`, `/jxjg/`, `/xysz/`
- Also check for links matching: `*.tsinghua.edu.cn` or `*.sysu.edu.cn` or `*.scu.edu.cn` subdomain patterns
- Check navigation elements (nav, menu, ul, li) for school/college links

**For faculty discovery** (second hop):

- Prefer links containing: teacher, faculty, staff, people, team, tutor, supervisor, professor, 师资, 教师, 教授, 导师, 师资队伍
- URLs matching patterns: `/faculty/`, `/teacher/`, `/staff/`, `/szdw/`, `/teacher/`, `/prof/`, `/people/`, `/team/`

**Domain handling**:

- Stay on the base domain and recognized subdomains (e.g., `*.university.edu.cn` is same-site for `university.edu.cn`).
- For Tsinghua specifically: allow `*.tsinghua.edu.cn` subdomains as same-site.
- For SYSU (中山大学): allow `*.sysu.edu.cn` subdomains as same-site.
- For SCU (四川大学): allow `*.scu.edu.cn` subdomains as same-site.

### Inclusion rules

When the page is a university portal with limited direct links, also look for these paths commonly used by Chinese universities:

- `/xygk/` or `/xygk.htm` - 院系概况 (school/department overview)
- `/szdw/` or `/szdw.htm` - 师资队伍 (faculty team)
- `/xxgk/` - 信息公开 (info disclosure - often has org structure)
- `/yxsz/` - 院系设置 (school/department setup)
- `/zzjg/` - 组织机构 (organizational structure)
- `/jgsz/` - 机构设置 (institution setup)
- `/xysz/` - 院系设置
- `/szdw/` - 师资队伍
- `/jxjg/` - 教学机构
- `/jxgl/` - 教学管理
- `/xx/` - 学校 (school path on some sites)
- `/xy/` - 学院 (college path on some sites)

### Exclusion rules

- News, notices, announcements, events (unless they contain faculty lists)
- Admissions, enrollment, student, alumni, career, job application pages
- Login, SSO, mail, library catalog, file download pages
- Social media links, external partner sites
- PDF/Word download links unless they are faculty CVs linked from a profile

### Chinese keyword priority list

- 学院 (college/school), 院系 (department/school), 系所 (department)
- 师资 (faculty), 教师 (teacher), 教授 (professor), 导师 (tutor/supervisor)
- 研究院 (research institute), 研究所 (institute), 实验室 (lab)
- 教学 (teaching), 科研 (research)
- 学部 (academic division/board), 机构 (organization), 设置 (setup)

### Handling JavaScript-heavy homepages

Some Chinese university homepages render navigation dynamically (JS menus). In such cases:

1. Check the page HTML source for hardcoded links in footer (footer, 底部)
2. Look for any visible `<a>` tags with relevant paths even if not in nav
3. Also check for links to known subdomain patterns that might be hardcoded
4. As fallback, the crawler should try common known paths directly (like `/xygk.htm`, `/szdw.htm`, `/yxsz.htm`)

### Usage

Call `extract_links` with `links`, `base_url`, and optional `keywords` to filter candidate navigation links.
