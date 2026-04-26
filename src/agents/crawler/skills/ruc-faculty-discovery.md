---
name: ruc-faculty-discovery
description: Discover faculty pages for Renmin University of China (中国人民大学) by navigating department subdomains and faculty listing pages.
version: 1
created_at: 2026-04-26T12:48:34+00:00
updated_at: 2026-04-26T12:48:34+00:00
---
## Renmin University of China (中国人民大学) Faculty Discovery

**Base domain**: `ruc.edu.cn`

**Key insight**: The main homepage (`www.ruc.edu.cn`) is heavily JavaScript-driven. The visible links on the homepage are mostly carousel rotator pages (`xiaoyuandaolan...`) and decorative pages (`xianshengyuanzhuo.html`). These do NOT lead to college or faculty listings. **Do not waste time on the homepage links.**

### Step 1: Bypass homepage — go directly to school/college subdomains

Use these direct subdomain URLs for colleges at RUC:

**Direct college/school subdomains:**

- `http://www.sf.ruc.edu.cn/` - School of Law (法学院)
- `http://ae.ruc.edu.cn/` - School of Applied Economics (应用经济学院)
- `http://se.ruc.edu.cn/` - School of Economics (经济学院)
- `http://www.sps.ruc.edu.cn/` - School of Philosophy (哲学院)
- `http://sls.ruc.edu.cn/` - School of Liberal Arts (文学院)
- `http://www.hic.ruc.edu.cn/` - School of History (历史学院)
- `http://www.sard.ruc.edu.cn/` - School of Agricultural Economics and Rural Development (农业与农村发展学院)
- `http://www.sis.ruc.edu.cn/` - School of International Studies (国际关系学院)
- `http://www.sociology.ruc.edu.cn/` - School of Sociology and Population Studies (社会与人口学院)
- `http://www.sme.ruc.edu.cn/` - School of Marxism (马克思主义学院)
- `http://jcr.ruc.edu.cn/` - School of Journalism and Communication (新闻学院)
- `http://www.sgg.ruc.edu.cn/` - School of Public Administration (公共管理学院)
- `http://www.spd.ruc.edu.cn/` - School of Party History and Party Building (中共党史党建学院)
- `http://si.ruc.edu.cn/` - School of Information (信息学院)
- `http://www.math.ruc.edu.cn/` - School of Mathematics (数学学院)
- `http://stat.ruc.edu.cn/` - School of Statistics (统计学院)
- `http://www.is.ruc.edu.cn/` - School of Information Resource Management (信息资源管理学院)
- `http://www.bss.ruc.edu.cn/` - School of Business / Business and Society (商学院)
- `http://www.sfs.ruc.edu.cn/` - School of Foreign Languages (外国语学院)
- `http://www.sard.ruc.edu.cn/` - School of Arts (艺术学院)
- `http://ai.ruc.edu.cn/` - School of AI (高瓴人工智能学院)
- `http://www.hss.ruc.edu.cn/` - School of Labor and Human Resources (劳动人事学院)
- `http://environment.ruc.edu.cn/` - School of Environment and Natural Resources (环境学院)
- `http://ygb.ruc.edu.cn/` - Graduate School

### Step 2: Find faculty pages on each college site

Look for links/paths on each college site containing:

- Chinese: 师资, 教师, 教授, 导师, 教工, 师资队伍, 人才培养, 研究生导师
- URL patterns: `/szdw/`, `/teacher/`, `/faculty/`, `/professor/`, `/ds/` (导师), `/szdw/`
- English: faculty, people, team, directory, staff

### Step 3: Specific known faculty page URLs to try

Try these directly on college subdomains:

- `http://www.sf.ruc.edu.cn/szdw/` - Law faculty
- `http://www.sf.ruc.edu.cn/szdw/jsxx.htm` - Law teacher info
- `http://se.ruc.edu.cn/szdw/` - Economics faculty
- `http://www.sps.ruc.edu.cn/szdw/` - Philosophy faculty
- `http://www.sociology.ruc.edu.cn/szdw/` - Sociology faculty
- `http://www.sis.ruc.edu.cn/szdw/` - International Studies faculty
- `http://si.ruc.edu.cn/szdw/` - Information faculty
- `http://stat.ruc.edu.cn/szdw/` - Statistics faculty
- `http://www.sfs.ruc.edu.cn/szdw/` - Foreign Languages faculty
- `http://ae.ruc.edu.cn/szdw/` - Applied Economics faculty
- `http://www.sme.ruc.edu.cn/szdw/` - Marxism faculty
- `http://jcr.ruc.edu.cn/szdw/` - Journalism faculty

Also try `/szdw/jsxx/` or `/szdw/jsxx.htm` (师资队伍/教师信息) and `/xygk/szdw/` patterns.

### Step 4: Extract professor info

- From listing pages: collect names, titles, research areas
- From individual profile pages: collect email, phone, bio, publications
- Save using `save_professors` with university_name="Renmin University of China" or "中国人民大学"

### Navigation rules

- Stay on `ruc.edu.cn` domain (including all subdomains like `*.ruc.edu.cn`)
- Prefer pages with Chinese faculty-related terms in path or title
- Avoid: news, admissions, student life, alumni, events, library, mail, login, career pages, and carousel/rotator pages

### Do NOT waste time on

- `xiaoyuandaolan*.html` - these are homepage carousel/rotator pages
- `xianshengyuanzhuo.html` - not a faculty listing page
- The main homepage itself — links there are mostly JS-generated or decorative

### Fallback strategy

If a college subdomain doesn't respond, check `http://www.ruc.edu.cn/` for a "学院" (colleges) section in the source HTML, or try to find an `http://www.ruc.edu.cn/xxgk/` (学校概况/about) or `http://www.ruc.edu.cn/yxsz/` (院系设置) page that lists all colleges with links.