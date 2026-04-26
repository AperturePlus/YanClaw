---
name: pku-faculty-discovery
description: Discover faculty pages for Peking University (北京大学) by navigating college/school subdomains directly.
version: 1
created_at: 2026-04-26T12:48:59+00:00
updated_at: 2026-04-26T12:48:59+00:00
---
# Peking University (北京大学) Faculty Discovery

**Base domain**: `pku.edu.cn`

**Critical insight**: The main PKU homepage (`www.pku.edu.cn`) and its department/shiYuan pages do NOT contain direct faculty listing links. These pages are more overview/administrative. **Do NOT waste time repeatedly crawling the homepage or its department listing pages.**

## PKU College Subdomain Structure

PKU colleges use the pattern: `[dept].pku.edu.cn` for subdomains.

### Direct college subdomain URLs to try:

**Humanities & Social Sciences:**
- `http://www.shxy.pku.edu.cn/` - School of Sociology
- `http://www.wxy.pku.edu.cn/` - Department of Chinese Language and Literature (中文系)
- `http://www.lsy.pku.edu.cn/` - Department of History (历史学系)
- `http://www.art.pku.edu.cn/` - School of Arts (艺术学院)
- `http://www.sfl.pku.edu.cn/` - School of Foreign Languages (外国语学院)
- `http://www.phil.pku.edu.cn/` - Department of Philosophy (哲学系)
- `http://www.jc.pku.edu.cn/` - School of Journalism and Communication (新闻与传播学院)

**Law, Economics & Management:**
- `http://www.law.pku.edu.cn/` - Law School (法学院)
- `http://econ.pku.edu.cn/` - School of Economics (经济学院)
- `http://www.gsm.pku.edu.cn/` - Guanghua School of Management (光华管理学院)
- `http://www.sg.pku.edu.cn/` - School of Government (政府管理学院)
- `http://www.sis.pku.edu.cn/` - School of International Studies (国际关系学院)
- `http://www.mse.pku.edu.cn/` - School of Marxism (马克思主义学院)

**Sciences & Engineering:**
- `http://www.math.pku.edu.cn/` - School of Mathematical Sciences (数学科学学院)
- `http://www.phy.pku.edu.cn/` - School of Physics (物理学院)
- `http://www.chem.pku.edu.cn/` - College of Chemistry (化学与分子工程学院)
- `http://www.bio.pku.edu.cn/` - School of Life Sciences (生命科学学院)
- `http://www.coe.pku.edu.cn/` - College of Engineering (工学院)
- `http://www.eecs.pku.edu.cn/` - School of Electronics Engineering and Computer Science (信息科学技术学院)
- `http://www.urban.pku.edu.cn/` - College of Urban and Environmental Sciences (城市与环境学院)
- `http://www.psy.pku.edu.cn/` - School of Psychological and Cognitive Sciences (心理与认知科学学院)
- `http://www.sess.pku.edu.cn/` - School of Earth and Space Sciences (地球与空间科学学院)
- `http://www.aais.pku.edu.cn/` - School of Advanced Agricultural Sciences (现代农业科学学院)
- `http://www.ces.pku.edu.cn/` - College of Environmental Sciences and Engineering (环境科学与工程学院)

**Medical & Health Sciences (including PKU Health Science Center):**
- Medical colleges are under `hsc.pku.edu.cn` subdomains

## Finding Faculty Pages

For each college subdomain, look for paths containing:
- Chinese: 师资队伍, 教师, 师资, 教授, 教工, 教师信息
- English: faculty, people, directory, professor, staff
- URL patterns: `/szdw/`, `/teachers/`, `/faculty/`, `/professor/`, `/szdw/jsxx/`

### Common faculty page paths to try on PKU college sites:
- `/szdw/` - 师资队伍
- `/szdw/jsxx/` - 师资队伍/教师信息
- `/szdw/zzjs/` - 师资队伍/在职教师
- `/teachers/`
- `/faculty/`
- `/professor/`
- `/xygk/szdw/` - 学院概况/师资队伍
- `/xygk/teachers/`

## Specific Known Faculty Pages

Some PKU colleges have known faculty page URLs:
- `http://www.law.pku.edu.cn/szdw/` - Law School faculty
- `http://econ.pku.edu.cn/szdw/` - Economics faculty
- `http://www.phy.pku.edu.cn/szdw/` - Physics faculty
- `http://www.math.pku.edu.cn/szdw/` - Math faculty
- `http://www.chem.pku.edu.cn/szdw/` - Chemistry faculty
- `http://www.sfl.pku.edu.cn/szdw/` - Foreign Languages faculty

## Navigation Strategy

1. **Skip the homepage** - Do not spend time on `www.pku.edu.cn`, `www.pku.edu.cn/department.html`, or `www.pku.edu.cn/shiYuan.html`
2. Go directly to college subdomains
3. On each college site, look for 师资队伍 (faculty) links
4. Follow those links to extract professor names, titles, etc.
5. If a listing page links to individual profiles, follow those for detailed info (email, bio, research areas)

## Save Format

Use `save_professors` with:
- university_name = "Peking University" or "北京大学"
- college_name = the specific college name