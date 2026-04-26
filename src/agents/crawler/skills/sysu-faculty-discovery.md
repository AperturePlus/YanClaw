---
name: sysu-faculty-discovery
description: Discover faculty pages for Sun Yat-sen University (中山大学) by navigating school/department listing pages and faculty directories.
version: 2
created_at: 2026-04-26T11:03:13
updated_at: 2026-04-26T11:37:43
---
## Sun Yat-sen University (中山大学) Faculty Discovery

**Base domain**: `sysu.edu.cn`

**Primary entry points**:

### Step 1: Try known direct URLs first

1. `https://www.sysu.edu.cn/xygk.htm` - 院系概况 (Schools Overview)
2. `https://www.sysu.edu.cn/szdw.htm` - 师资队伍 (Faculty)
3. `https://www.sysu.edu.cn/` - Homepage
4. `https://www.sysu.edu.cn/xy.htm` - 院系 (Schools)
5. `https://www.sysu.edu.cn/yxsz.htm` - 院系设置 (School setup)
6. `https://www.sysu.edu.cn/zzjg.htm` - 组织机构 (Organization)

### Step 2: Known schools/colleges subdomains (direct access)

- `chinese.sysu.edu.cn` - 中国语言文学系 (Chinese Language & Literature)
- `history.sysu.edu.cn` - 历史学系 (History)
- `philosophy.sysu.edu.cn` - 哲学系 (Philosophy)
- `ssa.sysu.edu.cn` - 社会学与人类学学院 (Sociology & Anthropology)
- `lingnan.sysu.edu.cn` - 岭南学院 (Lingnan College - Economics/Management)
- `bus.sysu.edu.cn` - 管理学院 (Business School)
- `law.sysu.edu.cn` - 法学院 (Law)
- `fl.sysu.edu.cn` - 外国语学院 (Foreign Languages)
- `math.sysu.edu.cn` - 数学学院 (Mathematics)
- `physics.sysu.edu.cn` - 物理学院 (Physics)
- `chem.sysu.edu.cn` - 化学学院 (Chemistry)
- `lssc.sysu.edu.cn` - 生命科学学院 (Life Sciences)
- `cs.sysu.edu.cn` - 计算机科学与工程学院 (Computer Science)
- `se.sysu.edu.cn` - 软件工程学院 (Software Engineering)
- `zssom.sysu.edu.cn` - 中山医学院 (Zhongshan Medical School)
- `sph.sysu.edu.cn` - 公共卫生学院 (Public Health)
- `pharmacy.sysu.edu.cn` - 药学院 (Pharmacy)
- `aoe.sysu.edu.cn` - 航空航天学院 (Aeronautics & Astronautics)
- `sdcs.sysu.edu.cn` - 数据科学与计算机学院 (Data Science)
- `geog.sysu.edu.cn` - 地理科学与规划学院 (Geography)
- `ses.sysu.edu.cn` - 环境科学与工程学院 (Environmental Science)
- `mse.sysu.edu.cn` - 材料科学与工程学院 (Materials)
- `eie.sysu.edu.cn` - 电子与信息工程学院 (Electronics & Information)
- `sps.sysu.edu.cn` - 政治与公共事务管理学院 (Political Science & Public Affairs)
- `gjxy.sysu.edu.cn` - 国际学院 (International)
- `nfgy.sysu.edu.cn` - 南方学院
- `sports.sysu.edu.cn` - 体育部 (Sports)

### Step 3: Find faculty pages within each school

Look for links containing:

- `/szdw/` or `/teacher/` - faculty listing
- `/prof/` - professors
- `/师资队伍/` - Chinese faculty page
- `/教师/` - teachers page
- `/jsxx/` - 教师信息
- `/dsjj/` - 导师简介
- `/szll/` - 师资力量

**Chinese keywords for faculty pages**:

- 教授 (Professor), 副教授 (Associate Professor), 讲师 (Lecturer)
- 博士生导师 (PhD supervisor), 硕士生导师 (Master's supervisor)
- 研究员 (Researcher), 副研究员 (Associate Researcher)
- 师资队伍 (Faculty Team), 师资力量 (Faculty)
- 在职教师 (Current Faculty), 全体教师 (All Teachers)
- 导师风采 (Tutor Profiles)

### Step 4: Extract professor info

- Save using `save_professors` with university_name="中山大学" or "Sun Yat-sen University"

### Navigation rules

- Stay on `sysu.edu.cn` domain (including subdomains `*.sysu.edu.cn`)
- Prefer pages with Chinese faculty-related terms in path or title
- Skip news (新闻), admissions (招生), student affairs (学生工作), events (活动)
- Skip hospital patient services, appointment booking pages
- Skip library, IT services, administrative systems

### Fallback strategy

If the homepage yields no links due to JavaScript, try known school subdomains directly and look for their faculty pages.
