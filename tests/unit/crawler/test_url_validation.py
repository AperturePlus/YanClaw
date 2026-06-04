from __future__ import annotations

from agents.crawler.agent import _is_category_name, _url_found_on_page
from agents.crawler.fetchers.link_signals import LinkSignal
from agents.crawler.url_heuristics import (
    FACULTY_PAGE_TYPE_CATEGORY,
    FACULTY_PAGE_TYPE_ELITE,
    FACULTY_PAGE_TYPE_FULL,
    FACULTY_PAGE_TYPE_NOISE,
    _assess_faculty_candidate,
    _assess_structural_faculty_candidates,
    _allow_faculty_candidate_for_org_unit,
    _is_non_faculty_noise_url,
    _is_faculty_platform,
    _is_query_profile_detail_url,
    _org_unit_exclusion_match,
    _should_exclude_org_unit,
    _looks_like_org_unit_listing_url,
    _looks_like_retired_content,
    _rank_faculty_page_candidates,
    _select_balanced_faculty_candidates,
)


def test_url_found_exact_match_in_links():
    links = {"https://cs.scu.edu.cn/", "https://math.scu.edu.cn/"}
    assert _url_found_on_page("https://cs.scu.edu.cn/", links, "")


def test_url_not_found_when_absent():
    links = {"https://cs.scu.edu.cn/"}
    assert not _url_found_on_page("https://fake.scu.edu.cn/yxsz/fxy", links, "")


def test_url_found_by_hostname_path_match():
    """Trailing slash differences should not prevent matching."""
    links = {"https://cs.scu.edu.cn/szdw/"}
    assert _url_found_on_page("https://cs.scu.edu.cn/szdw", links, "")


def test_url_found_by_hostname_in_text():
    """Hostname appearing in page text is a valid fallback."""
    text = "计算机学院 cs.scu.edu.cn 欢迎您"
    assert _url_found_on_page("https://cs.scu.edu.cn/", set(), text.lower())


def test_url_not_found_hostname_absent_from_text():
    text = "四川大学 www.scu.edu.cn 首页"
    assert not _url_found_on_page("https://fake.scu.edu.cn/", set(), text.lower())


def test_hallucinated_url_rejected():
    """A URL fabricated by the LLM (not on the page) should be rejected."""
    links = {
        "https://cs.scu.edu.cn/",
        "https://math.scu.edu.cn/",
        "https://law.scu.edu.cn/",
    }
    text = "cs.scu.edu.cn math.scu.edu.cn law.scu.edu.cn"
    # This URL pattern (www.scu.edu.cn/yxsz/jsjxy) is a known hallucination
    assert not _url_found_on_page(
        "https://www.scu.edu.cn/yxsz/jsjxy", links, text.lower()
    )



def test_category_name_rejected():
    """Section headings like '教学科研单位、研究机构' are not real org units."""
    assert _is_category_name("教学科研单位、研究机构")
    assert _is_category_name("国家级科研机构")
    assert _is_category_name("独立学院")
    assert _is_category_name("直属单位")
    assert _is_category_name("党群部门")


def test_real_college_name_accepted():
    """Actual college/department names should pass."""
    assert not _is_category_name("计算机科学与工程学院")
    assert not _is_category_name("数学学院")
    assert not _is_category_name("法学院")
    assert not _is_category_name("华西临床医学院")
    assert not _is_category_name("信息与通信工程学院")


def test_org_unit_exclusion_hard_rules_match_blacklisted_units():
    assert _should_exclude_org_unit(name="艺术学院", url="https://art.example.edu.cn/")
    assert _should_exclude_org_unit(name="体育学院", url="https://sports.example.edu.cn/")
    assert _should_exclude_org_unit(name="中外合作办学学院", url="https://joint.example.edu.cn/")
    assert _should_exclude_org_unit(name="国际联合学院", url="https://joint.example.edu.cn/")
    assert _should_exclude_org_unit(name="基教中心", url="https://basic.example.edu.cn/")
    assert _should_exclude_org_unit(name="基础教学部", url="https://basic.example.edu.cn/")
    assert _should_exclude_org_unit(name="继续教育学院", url="https://jxjy.example.edu.cn/")
    assert _should_exclude_org_unit(name="成人教育学院", url="https://adult.example.edu.cn/")
    assert _should_exclude_org_unit(name="网络教育学院", url="https://online.example.edu.cn/")
    assert _should_exclude_org_unit(name="吴玉章书院", url="https://wyz.example.edu.cn/")
    assert _should_exclude_org_unit(name="北航学院", url="https://bhxy.buaa.edu.cn/")
    assert _should_exclude_org_unit(name="卓越工程师学院", url="https://engineer.example.edu.cn/")
    assert _should_exclude_org_unit(name="卓工学院", url="https://elite.example.edu.cn/")
    assert _org_unit_exclusion_match(name="格拉斯哥学院", url="https://glasgow.example.edu.cn/").category == "joint_program"
    assert (
        _org_unit_exclusion_match(name="继续教育学院", url="https://jxjy.example.edu.cn/").category
        == "continuing_education"
    )
    assert (
        _org_unit_exclusion_match(name="吴玉章书院", url="https://wyz.example.edu.cn/").category
        == "undergraduate_teaching_unit"
    )
    assert (
        _org_unit_exclusion_match(name="卓越工程师学院", url="https://engineer.example.edu.cn/").category
        == "excellent_engineer_program"
    )


def test_org_unit_exclusion_hard_rules_avoid_false_positives():
    assert not _should_exclude_org_unit(name="人工智能学院", url="https://ai.example.edu.cn/")
    assert not _should_exclude_org_unit(name="工业设计学院", url="https://design.example.edu.cn/")
    assert not _should_exclude_org_unit(name="经济管理学院", url="https://sem.example.edu.cn/")
    assert not _should_exclude_org_unit(name="医学院", url="https://med.example.edu.cn/")
    assert not _should_exclude_org_unit(name="农学院", url="https://agri.example.edu.cn/")
    assert not _should_exclude_org_unit(name="外国语学院", url="https://foreign.example.edu.cn/")
    assert not _should_exclude_org_unit(name="教育学院", url="https://edu.example.edu.cn/")
    assert not _should_exclude_org_unit(name="高等教育研究院", url="https://ihe.example.edu.cn/")
    assert not _should_exclude_org_unit(name="航空学院", url="https://aviation.example.edu.cn/")
    assert not _should_exclude_org_unit(name="软件学院", url="https://software.example.edu.cn/")


def test_retired_content_not_triggered_on_mixed_faculty_tabs():
    text = (
        "\u5e08\u8d44\u961f\u4f0d\n"
        "\u5728\u804c\u6559\u5e08\n"
        "\u8363\u4f11\u6559\u5e08\n"
        "\u6559\u5e08\u540d\u5f55\n"
    )
    assert not _looks_like_retired_content(text, "https://example.edu.cn/szdw.htm")


def test_retired_content_triggered_for_retired_only_page():
    text = (
        "\u8363\u4f11\u6559\u804c\u5de5\n"
        "\u79bb\u9000\u4f11\u4eba\u5458\n"
        "\u9000\u4f11\u6559\u5e08\n"
    )
    assert _looks_like_retired_content(text, "https://example.edu.cn/szdw/rxjzg.htm")


def test_org_unit_listing_url_filters_non_academic_pages():
    assert _looks_like_org_unit_listing_url("https://www.scu.edu.cn/zzjg1/yx.htm")
    assert not _looks_like_org_unit_listing_url("https://www.scu.edu.cn/xxgk/xxjj.htm")
    assert not _looks_like_org_unit_listing_url("https://www.scu.edu.cn/zzjg1/jgbc.htm")


def test_non_faculty_noise_url_matches_rszc_variants():
    assert _is_non_faculty_noise_url("https://www.example.edu.cn/szdw/rszc.htm")
    assert _is_non_faculty_noise_url("https://www.example.edu.cn/szdw/rszc/4.htm")
    assert _is_non_faculty_noise_url("https://www.example.edu.cn/szdw/rszc2.htm")


def test_non_faculty_noise_url_does_not_block_regular_faculty_paths():
    assert not _is_non_faculty_noise_url("https://www.example.edu.cn/szdw/jsdw.htm")
    assert not _is_non_faculty_noise_url("https://www.example.edu.cn/faculty/teacher_list.htm")


def test_non_faculty_noise_url_ignores_news_token_in_query_string():
    # BUAA siteweaver detail URLs (`teachershouw.jsp?urltype=news.NewsContentUrl&...`)
    # were misclassified as noise because the query string carried the literal
    # token "news"; only the path should drive noise classification.
    assert not _is_non_faculty_noise_url(
        "https://soft.buaa.edu.cn/teachershouw.jsp?urltype=news.NewsContentUrl&wbtreeid=1262&wbnewsid=9633"
    )
    # Path-based news pages must still be flagged.
    assert _is_non_faculty_noise_url(
        "https://soft.buaa.edu.cn/news_list.jsp?urltype=tree.TreeTempUrl&wbtreeid=1078"
    )


def test_query_profile_detail_url_detects_scu_teamlist_detail_only():
    assert _is_query_profile_detail_url(
        "https://saa.scu.edu.cn/teamlist.htm?action=detailTeam&uuinId=661618903336854"
    )
    assert not _is_query_profile_detail_url("https://saa.scu.edu.cn/teamlist.htm")
    assert not _is_query_profile_detail_url("https://saa.scu.edu.cn/teamlist.htm?uuinUuteId=1761503632748933")
    assert not _is_query_profile_detail_url(
        "https://saa.scu.edu.cn/list.htm?m=1351479452361353&c=661618903336591&currentPage=1"
    )


def test_faculty_platform_detects_teacher_and_faculty_subdomains():
    assert _is_faculty_platform("https://teacher.buaa.edu.cn/")
    assert _is_faculty_platform("https://faculty.buaa.edu.cn/")
    assert not _is_faculty_platform("https://scse.buaa.edu.cn/")


def test_allow_faculty_candidate_for_org_unit_strict_host_gate():
    start_url = "https://www.buaa.edu.cn/"
    org_unit_url = "https://soft.buaa.edu.cn/"

    assert _allow_faculty_candidate_for_org_unit(
        "https://soft.buaa.edu.cn/szdw/jsdw.htm",
        org_unit_url=org_unit_url,
        start_url=start_url,
    )
    assert _allow_faculty_candidate_for_org_unit(
        "https://www.buaa.edu.cn/szdw/jsdw.htm",
        org_unit_url=org_unit_url,
        start_url=start_url,
    )
    assert not _allow_faculty_candidate_for_org_unit(
        "https://teacher.buaa.edu.cn/",
        org_unit_url=org_unit_url,
        start_url=start_url,
    )
    assert not _allow_faculty_candidate_for_org_unit(
        "https://scse.buaa.edu.cn/szdw/jsdw.htm",
        org_unit_url=org_unit_url,
        start_url=start_url,
    )


def test_rank_faculty_page_candidates_tie_break_is_stable():
    a = "https://www.example.edu.cn/cs/teacher/list.htm"
    b = "https://www.example.edu.cn/cs/teacher/index.htm"
    c = "https://www.example.edu.cn/cs/teacher/overview.htm"
    ranked_one = _rank_faculty_page_candidates([c, a, b])
    ranked_two = _rank_faculty_page_candidates([b, c, a])
    assert ranked_one == ranked_two


def test_assess_faculty_candidate_classifies_noise_or_login_hard_reject():
    item = _assess_faculty_candidate(
        "https://scse.buaa.edu.cn/system/resource/tplloginaccount.jsp?owner=1"
    )
    assert item.page_type == FACULTY_PAGE_TYPE_NOISE
    assert item.hard_reject is True
    assert item.score < 0


def test_assess_faculty_candidate_classifies_full_category_and_elite():
    full = _assess_faculty_candidate(
        "https://scse.buaa.edu.cn/szdw/jsdw.htm",
        anchor_text="全体教师",
        heading_text="师资队伍",
    )
    category = _assess_faculty_candidate(
        "https://scse.buaa.edu.cn/szdw/js.htm",
        anchor_text="教授",
        heading_text="教师名录",
    )
    elite = _assess_faculty_candidate(
        "https://scse.buaa.edu.cn/szdw/jcrc.htm",
        anchor_text="杰出人才",
        heading_text="高层次人才",
    )
    assert full.page_type == FACULTY_PAGE_TYPE_FULL
    assert category.page_type == FACULTY_PAGE_TYPE_CATEGORY
    assert elite.page_type == FACULTY_PAGE_TYPE_ELITE


def test_select_balanced_candidates_drops_elite_when_full_exists():
    assessed = _assess_structural_faculty_candidates(
        [
            "https://scse.buaa.edu.cn/szdw/jsdw.htm",
            "https://scse.buaa.edu.cn/szdw/js.htm",
            "https://scse.buaa.edu.cn/szdw/jcrc.htm",
        ],
        link_signals=(
            LinkSignal(
                url="https://scse.buaa.edu.cn/szdw/jsdw.htm",
                anchor_text="全体教师",
                heading_text="师资队伍",
                parent_tags_or_classes=("nav.menu",),
                link_order=1,
            ),
            LinkSignal(
                url="https://scse.buaa.edu.cn/szdw/js.htm",
                anchor_text="教授",
                heading_text="教师名录",
                parent_tags_or_classes=("nav.menu",),
                link_order=2,
            ),
            LinkSignal(
                url="https://scse.buaa.edu.cn/szdw/jcrc.htm",
                anchor_text="杰出人才",
                heading_text="高层次人才",
                parent_tags_or_classes=("nav.menu",),
                link_order=3,
            ),
        ),
    )
    selected = _select_balanced_faculty_candidates(assessed, limit=4)
    selected_urls = [item.url for item in selected]
    assert "https://scse.buaa.edu.cn/szdw/jsdw.htm" in selected_urls
    assert "https://scse.buaa.edu.cn/szdw/js.htm" in selected_urls
    assert "https://scse.buaa.edu.cn/szdw/jcrc.htm" not in selected_urls


def test_buaa_active_teacher_roster_is_selected_before_elite_talent_page():
    roster_url = "https://dept3.buaa.edu.cn/szjs/zzjs/znxtykzgcx.htm"
    elite_url = "https://dept3.buaa.edu.cn/szjs/jcrc.htm"
    mentor_url = "https://dept3.buaa.edu.cn/szjs/yjsds.htm"
    assessed = _assess_structural_faculty_candidates(
        [elite_url, mentor_url, roster_url],
        link_signals=(
            LinkSignal(
                url=elite_url,
                anchor_text="杰出人才",
                heading_text="师资建设",
                parent_tags_or_classes=("nav.menu",),
                link_order=1,
            ),
            LinkSignal(
                url=mentor_url,
                anchor_text="研究生导师",
                heading_text="师资建设",
                parent_tags_or_classes=("nav.menu",),
                link_order=2,
            ),
            LinkSignal(
                url=roster_url,
                anchor_text="在职教师",
                heading_text="师资建设",
                parent_tags_or_classes=("nav.menu",),
                link_order=3,
            ),
        ),
    )

    roster = next(item for item in assessed if item.url == roster_url)
    selected_urls = [item.url for item in _select_balanced_faculty_candidates(assessed, limit=4)]

    assert roster.page_type == FACULTY_PAGE_TYPE_FULL
    assert roster_url in selected_urls
    assert elite_url not in selected_urls


def test_assess_structural_candidates_tie_break_is_stable_without_signals():
    links_one = [
        "https://scse.buaa.edu.cn/szdw/jsdw/c.htm",
        "https://scse.buaa.edu.cn/szdw/jsdw/a.htm",
        "https://scse.buaa.edu.cn/szdw/jsdw/b.htm",
    ]
    links_two = [
        "https://scse.buaa.edu.cn/szdw/jsdw/b.htm",
        "https://scse.buaa.edu.cn/szdw/jsdw/c.htm",
        "https://scse.buaa.edu.cn/szdw/jsdw/a.htm",
    ]

    assessed_one = _assess_structural_faculty_candidates(links_one, link_signals=())
    assessed_two = _assess_structural_faculty_candidates(links_two, link_signals=())

    ordered_one = [item.url for item in assessed_one]
    ordered_two = [item.url for item in assessed_two]
    assert ordered_one == ordered_two
