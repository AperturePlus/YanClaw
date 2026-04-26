---
name: buaa-faculty-discovery
description: Discover faculty pages for Beihang University (北京航空航天大学) by navigating school/department subdomains and faculty listing pages.
version: 1
created_at: 2026-04-26T11:39:09
updated_at: 2026-04-26T11:39:09
---
## Beihang University (北京航空航天大学) Faculty Discovery

**Base domain**: `buaa.edu.cn`

**Primary entry points** (try in order):

### Step 1: Try known direct URLs first

Since the homepage uses JavaScript-heavy navigation, try these directly:

1. `https://www.buaa.edu.cn/` - Homepage (check footer for hardcoded links)
2. `https://www.buaa.edu.cn/xxgk/xxgk.htm` - 学校概况 (About)
3. `https://www.buaa.edu.cn/jgsz/jgsz.htm` - 机构设置 (Organization)
4. `https://www.buaa.edu.cn/szdw/szdw.htm` - 师资队伍 (Faculty)
5. `https://www.buaa.edu.cn/xy/xy.htm` - 院系 (Schools/Colleges)
6. `https://www.buaa.edu.cn/yxsz/yxsz.htm` - 院系设置 (School Setup)
7. `https://www.buaa.edu.cn/zzjg/zzjg.htm` - 组织机构 (Org Structure)

### Step 2: Known school subdomains (direct access)

Try these well-known BUAA school subdomains directly:

**Engineering & Sciences:**

- `https://www.sem.buaa.edu.cn/` - School of Economics & Management (经济管理学院)
- `https://www.cs.buaa.edu.cn/` - School of Computer Science & Engineering (计算机学院)
- `https://www.ee.buaa.edu.cn/` - School of Electronic & Information Engineering (电子信息工程学院)
- `https://www.me.buaa.edu.cn/` - School of Mechanical Engineering & Automation (机械工程及自动化学院)
- `https://www.sa.buaa.edu.cn/` - School of Astronautics (宇航学院)
- `https://www.ase.buaa.edu.cn/` - School of Aeronautic Science & Engineering (航空科学与工程学院)
- `https://www.mat.buaa.edu.cn/` - School of Materials Science & Engineering (材料科学与工程学院)
- `https://www.sse.buaa.edu.cn/` - School of Software (软件学院)
- `https://www.sci.buaa.edu.cn/` - School of Science (理学院)
- `https://www.uaa.buaa.edu.cn/` - School of Instrumentation & Optoelectronic Engineering (仪器科学与光电工程学院)
- `https://www.ce.buaa.edu.cn/` - School of Civil Engineering (土木工程学院)
- `https://www.energy.buaa.edu.cn/` - School of Energy & Power Engineering (能源与动力工程学院)
- `https://www.automation.buaa.edu.cn/` - School of Automation Science & Electrical Engineering (自动化科学与电气工程学院)
- `https://www.traffic.buaa.edu.cn/` - School of Transportation Science & Engineering (交通科学与工程学院)
- `https://www.bme.buaa.edu.cn/` - School of Biological Science & Medical Engineering (生物与医学工程学院)
- `https://www.spa.buaa.edu.cn/` - School of Physics (物理学院)
- `https://www.math.buaa.edu.cn/` - School of Mathematics (数学科学学院)
- `https://www.chem.buaa.edu.cn/` - School of Chemistry (化学学院)

**Humanities & Social Sciences:**

- `https://www.law.buaa.edu.cn/` - School of Law (法学院)
- `https://www.sfl.buaa.edu.cn/` - School of Foreign Languages (外国语学院)
- `https://www.hss.buaa.edu.cn/` - School of Humanities & Social Sciences (人文社会科学学院)
- `https://www.art.buaa.edu.cn/` - School of Arts (新媒体艺术与设计学院)
- `https://www.mks.buaa.edu.cn/` - School of Marxism (马克思主义学院)
- `https://www.sf.buaa.edu.cn/` - School of Public Administration (公共管理学院)
- `https://www.soe.buaa.edu.cn/` - School of Economics (经济学院)

**Other notable:**

- `https://www.gs.buaa.edu.cn/` - Graduate School (研究生院)
- `https://www.news.buaa.edu.cn/` - School of Journalism (新闻学院 - if exists)
- `https://www.ir.buaa.edu.cn/` - Institute of International Relations
- `https://www.sme.buaa.edu.cn/` - School of Microelectronics (微电子学院)
- `https://www.nlsde.buaa.edu.cn/` - School of Network & Software Engineering (网络空间安全学院)
- `https://www.integrated.buaa.edu.cn/` - School of Integrated Circuits (集成电路科学与工程学院)
- `https://www.ai.buaa.edu.cn/` - School of Artificial Intelligence (人工智能研究院)
- `https://www.env.buaa.edu.cn/` - School of Environment (环境学院)

### Step 3: Find faculty pages within each school

Look for links containing:

- 师资, 教师, 教授, 导师, 教工, 师资队伍
- URL patterns: `/szdw/`, `/teacher/`, `/faculty/`, `/professor/`, `/dsjj/` (导师简介)
- `/szdw/jsxx.htm` - 师资队伍/教师信息
- `/szdw/zzjs.htm` - 师资队伍/在职教师
- `/xygk/szdw.htm` - 院系概况/师资队伍
- `/szdw/szll.htm` - 师资力量
- `/szdw/bshds.htm` - 博士生导师
- `/szdw/ssds.htm` - 硕士生导师

**Common BUAA faculty page URL patterns:**

- `https://cs.buaa.edu.cn/szdw.htm` or `/szdw/`
- `https://cs.buaa.edu.cn/teacher/`
- `https://cs.buaa.edu.cn/jiaoshou/` (教授)
- `https://cs.buaa.edu.cn/faculty/`
- `https://cs.buaa.edu.cn/xygk/szdw/`
- `https://cs.buaa.edu.cn/szdw/jsxx/`

### Step 4: Extract professor info

- From listing pages: collect names, titles, research areas
- From individual profile pages: collect email, phone, bio, publications
- Save using `save_professors` with university_name="北京航空航天大学" or "Beihang University"

### Navigation rules

- Stay on `buaa.edu.cn` domain (including subdomains like `*.buaa.edu.cn`)
- Prefer pages with Chinese faculty-related terms in path or title
- Avoid: news (新闻), admissions (招生), student affairs (学生工作), events (活动)
- Avoid: library, IT services, administrative systems, login pages

### Fallback strategy

If the homepage yields no links due to JavaScript:

1. Check the page HTML source footer for hardcoded school links
2. Try known school subdomains directly (Step 2)
3. For each school subdomain, try common faculty page URL patterns (Step 3)
4. If a school subdomain doesn't respond, try `https://www.buaa.edu.cn/xy/` + school name in pinyin
