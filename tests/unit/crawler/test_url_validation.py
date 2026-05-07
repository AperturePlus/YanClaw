from __future__ import annotations

from agents.crawler.agent import _is_category_name, _url_found_on_page
from agents.crawler.url_heuristics import (
    _is_non_faculty_noise_url,
    _looks_like_org_unit_listing_url,
    _looks_like_retired_content,
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
