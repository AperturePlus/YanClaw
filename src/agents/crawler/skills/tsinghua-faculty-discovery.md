---
name: tsinghua-faculty-discovery
description: Discover faculty pages for Tsinghua University by navigating department/school subdomains and faculty listing pages.
version: 3
created_at: 2026-04-26T10:43:43
updated_at: 2026-04-26T12:49:57+00:00
---
## Tsinghua University Faculty Discovery

**Base domain**: `tsinghua.edu.cn`

**General principle**: Many Chinese university homepages use JavaScript-heavy navigation (carousels, sliders, decorative pages) that leads to dead ends. If the homepage yields no college links after 1-2 attempts, **skip the homepage** and go directly to known school subdomains.

**Primary entry points** (try in order):

### Step 1: Known direct faculty/college listing URLs

Try these URLs directly since the homepage may use JavaScript-heavy navigation:

1. **师资队伍 (Faculty)** - `https://www.tsinghua.edu.cn/szdw.htm`
2. **院系设置 (Schools/Colleges listing)** - `https://www.tsinghua.edu.cn/yxsz.htm`
3. **学校概况 (About/Overview)** - `https://www.tsinghua.edu.cn/xxgk/xxgk.htm`
4. **组织机构 (Organization)** - `https://www.tsinghua.edu.cn/zzjg/zzjg.htm`
5. **教学单位 (Teaching Units)** - `https://www.tsinghua.edu.cn/jxgl/jxgl.htm`

### Step 2: Known school subdomains (direct access)

Try these well-known Tsinghua school subdomains directly:

- `https://www.cs.tsinghua.edu.cn/` - Computer Science (计算机系)
- `https://www.ee.tsinghua.edu.cn/` - Electronic Engineering (电子系)
- `https://www.soft.tsinghua.edu.cn/` - Software (软件学院)
- `https://www.sem.tsinghua.edu.cn/` - Economics & Management (经管学院)
- `https://www.arch.tsinghua.edu.cn/` - Architecture (建筑学院)
- `https://www.dpi.tsinghua.edu.cn/` - Public Policy (公管学院)
- `https://www.math.tsinghua.edu.cn/` - Mathematics (数学系)
- `https://www.phys.tsinghua.edu.cn/` - Physics (物理系)
- `https://www.chem.tsinghua.edu.cn/` - Chemistry (化学系)
- `https://www.biology.tsinghua.edu.cn/` - Biology (生命学院)
- `https://www.mae.tsinghua.edu.cn/` - Mechanical Engineering (机械系)
- `https://www.ep.tsinghua.edu.cn/` - Electrical Engineering (电机系)
- `https://www.civil.tsinghua.edu.cn/` - Civil Engineering (土木系)
- `https://www.media.tsinghua.edu.cn/` - Journalism & Communication (新闻学院)
- `https://www.law.tsinghua.edu.cn/` - Law (法学院)
- `https://www.ling.tsinghua.edu.cn/` - Foreign Languages (外文系)
- `https://www.history.tsinghua.edu.cn/` - History (历史系)
- `https://www.phil.tsinghua.edu.cn/` - Philosophy (哲学系)
- `https://www.sociology.tsinghua.edu.cn/` - Sociology (社会学系)
- `https://www.ad.tsinghua.edu.cn/` - Art & Design (美术学院)
- `https://www.gs.tsinghua.edu.cn/` - Graduate School (研究生院)
- `https://www.rim.tsinghua.edu.cn/` - Research Institutes (研究机构)
- `https://www.med.tsinghua.edu.cn/` - Medicine (医学院)
- `https://www.ins.tsinghua.edu.cn/` - Institute for Nuclear and New Energy Technology (核研院)
- `https://www.sppm.tsinghua.edu.cn/` - School of Public Policy and Management (公管学院)
- `https://www.yls.tsinghua.edu.cn/` - Yung Law School (法学院英文版)

### Step 3: Find faculty pages within each school

On each school's website, look for links containing:

- 师资, 教师, 教授, 导师, 教工, 师资队伍
- URL patterns: `/szdw/`, `/teacher/`, `/faculty/`, `/professor/`, `/dsjj/` (导师简介)
- English: faculty, people, team, staff

### Step 4: Extract professor info

- From listing pages: collect names, titles, research areas
- From individual profile pages: collect email, phone, bio, publications
- Save using `save_professors` with university_name="Tsinghua University" or "清华大学"

### Navigation rules

- Stay on `tsinghua.edu.cn` domain (including all subdomains like `*.tsinghua.edu.cn`)
- Prefer pages with Chinese faculty-related terms in path or title
- Avoid: news, admissions, student life, alumni, events, library, mail, login pages

### Common Chinese faculty page URL patterns

- `/szdw/` or `/szdw.htm` - 师资队伍
- `/jsxx/` or `/jsxx.htm` - 教师信息
- `/dsjj/` or `/dsjj.htm` - 导师简介
- `/szdw/jsxx/` - 师资队伍/教师信息
- `/prof/` - professors
- `/teacher/` - teachers
- `/faculty/` - faculty
- `/people/` - people/directory
- `/xygk/szdw/` - 院系概况/师资队伍
- `/szdwxz/` - 师资队伍/小组
- `/jstd/` - 教师团队
- `/jsml/` - 教师名录

### If a school subdomain doesn't work

Try `https://www.tsinghua.edu.cn/` + school path:

- `https://www.tsinghua.edu.cn/xx/` - info school paths
- `https://www.tsinghua.edu.cn/yx/` - school listing

### Fallback strategy

If the above specific URLs fail, check the homepage HTML source (not rendered) for hardcoded links to school pages in the footer or hidden navigation. Many Chinese university homepages have hardcoded school/faculty links in the footer (底部链接). If after 2 passes the homepage only yields carousel/slider links and no college links, **abandon the homepage** and try subdomains directly.