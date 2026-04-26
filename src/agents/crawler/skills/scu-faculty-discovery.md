---
name: scu-faculty-discovery
description: Discover faculty pages for Sichuan University (scu.edu.cn) by navigating the homepage, organization structure, and school/department faculty listing pages.
version: 2
created_at: 2026-04-26T11:03:28
updated_at: 2026-04-26T11:37:26
---
## Sichuan University (SCU) Faculty Discovery

**Base domain**: `scu.edu.cn`

**Primary entry points** (try in order):

### Step 1: Try known direct URLs first

Since the homepage may use JavaScript-heavy navigation, try these directly:

1. `https://www.scu.edu.cn/szdw/szdw.htm` - 师资队伍 (Faculty/Staff page)
2. `https://www.scu.edu.cn/xxgk/xxgk.htm` - 学校概况 (School Overview)
3. `https://www.scu.edu.cn/` - Homepage (look for 机构设置, 院系设置, 教学单位 links)
4. `https://www.scu.edu.cn/xygk/` - 院系概况 (School overview)
5. `https://www.scu.edu.cn/yxsz/` - 院系设置 (School setup)

### Step 2: Known school subdomains (direct access)

- `cs.scu.edu.cn` (Computer Science - 计算机学院)
- `math.scu.edu.cn` (Mathematics - 数学学院)
- `physics.scu.edu.cn` (Physics - 物理学院)
- `chem.scu.edu.cn` (Chemistry - 化学学院)
- `eie.scu.edu.cn` (Electrical & Information - 电气信息学院)
- `me.scu.edu.cn` (Mechanical Engineering - 机械工程学院)
- `mse.scu.edu.cn` (Materials Science - 材料科学与工程学院)
- `life.scu.edu.cn` (Life Sciences - 生命科学学院)
- `wcs.scu.edu.cn` (West China School - 华西)
- `med.scu.edu.cn` (Medical - 医学中心)
- `wcphs.scu.edu.cn` (West China Public Health - 华西公共卫生学院)
- `wcsz.scu.edu.cn` (West China Stomatology - 华西口腔医学院)
- `wcyx.scu.edu.cn` (West China Medicine - 华西临床医学院)
- `ce.scu.edu.cn` (Chemical Engineering - 化工学院)
- `env.scu.edu.cn` (Environment - 环境学院)
- `history.scu.edu.cn` (History - 历史文化学院)
- `lit.scu.edu.cn` (Literature - 文学与新闻学院)
- `law.scu.edu.cn` (Law - 法学院)
- `gg.scu.edu.cn` (Public Administration - 公共管理学院)
- `jj.scu.edu.cn` (Economics - 经济学院)
- `sfl.scu.edu.cn` (Foreign Languages - 外国语学院)
- `art.scu.edu.cn` (Arts - 艺术学院)
- `mks.scu.edu.cn` (Marxism - 马克思主义学院)
- `sports.scu.edu.cn` (Sports - 体育学院)
- `jwc.scu.edu.cn` (Academic Affairs - 教务处)
- `gs.scu.edu.cn` (Graduate School - 研究生院)

### Step 3: Find faculty pages within each school

- Look for links containing: 师资, 教师, 教授, 导师, 教工, 师资队伍
- URL patterns: `/szdw/`, `/teacher/`, `/faculty/`, `/professor/`, `/dsjj/` (导师简介)
- Chinese keywords: 师资队伍 (faculty roster), 教师介绍 (teacher intro), 导师风采 (tutor profile)

**Common SCU faculty page URL patterns:**

- `/szdw/jsxx.htm` - 师资队伍/教师信息
- `/szdw/zzjs.htm` - 师资队伍/在职教师
- `/xygk/szdw.htm` - 院系概况/师资队伍
- `/szdw/szll.htm` - 师资队伍/师资力量
- `/szdw/bshds.htm` - 师资队伍/博士生导师

### Step 4: Extract professor info

- From listing pages: collect names, titles, research areas
- From individual profile pages: collect email, phone, bio, publications
- Save using `save_professors` with university_name="四川大学" or "Sichuan University"

### Navigation rules

- Stay on `scu.edu.cn` domain (including subdomains like `*.scu.edu.cn`)
- Prefer pages with Chinese faculty-related terms in path or title
- Avoid: news, admissions, student life, alumni, events, library, mail

### Known URL patterns

- School page: `https://cs.scu.edu.cn/` or `https://www.scu.edu.cn/xy/xxx/`
- Faculty list: `https://cs.scu.edu.cn/szdw.htm` or `https://cs.scu.edu.cn/teacher/`
- Profile page: `https://cs.scu.edu.cn/teacher/xxx.htm`

### Fallback strategy

If the homepage yields no links due to JavaScript, try known school subdomains directly and look for their faculty pages.
