from __future__ import annotations

import ssl

from agents.crawler.fetchers.httpx_fetcher import (
    Fetcher,
    _detect_block_reason,
    _is_html_content,
    _is_ssl_error,
)
from agents.crawler.fetchers.link_signals import extract_links_with_signals


def test_fetcher_utils_convert_html_and_extract_links():
    helper = Fetcher()
    html = '<html><body><a href="/faculty">Faculty</a><p>Hello</p></body></html>'

    text = helper._html_to_text(html)
    links = helper._extract_links(html, "https://www.example.edu.cn/")

    assert "Hello" in text
    assert "https://www.example.edu.cn/faculty" in links


def test_fetcher_utils_extract_links_dedup_and_filter_non_http():
    helper = Fetcher()
    html = """
    <html><body>
      <a href="/a">A</a>
      <a href="/a">A2</a>
      <a href="mailto:test@example.edu.cn">mail</a>
      <a href="javascript:void(0)">js</a>
      <a href="https://www.example.edu.cn/b">B</a>
    </body></html>
    """
    links = helper._extract_links(html, "https://www.example.edu.cn/")
    assert links == [
        "https://www.example.edu.cn/a",
        "https://www.example.edu.cn/b",
    ]


def test_extract_links_rejects_cms_html_error_href_but_keeps_chinese_paths():
    helper = Fetcher()
    bad_href = "<span style='color:red;font-size:9pt'>\u8f6c\u6362\u94fe\u63a5\u9519\u8bef</span"
    valid_path = "/\u5e08\u8d44/\u6559\u5e08.htm"
    html = f'<html><body><a href="{bad_href}">bad</a><a href="{valid_path}">ok</a></body></html>'

    links = helper._extract_links(html, "https://www.example.edu.cn/szdw/")

    assert links == ["https://www.example.edu.cn/\u5e08\u8d44/\u6559\u5e08.htm"]


def test_filter_same_domain_rejects_encoded_cms_html_error_url_and_keeps_valid_urls():
    bad_url = (
        "https://sesu.scu.edu.cn/szdw/zzjs1/"
        "%3Cspan%20style='color:red;font-size:9pt'%3E"
        "%E8%BD%AC%E6%8D%A2%E9%93%BE%E6%8E%A5%E9%94%99%E8%AF%AF%3C/span"
    )
    valid_url = "https://sesu.scu.edu.cn/szdw/wkjxjs.htm"

    assert Fetcher.filter_same_domain([bad_url, valid_url], "https://www.scu.edu.cn/") == [valid_url]


def test_structural_link_signals_reject_cms_html_error_href():
    bad_href = "<span style='color:red;font-size:9pt'>\u8f6c\u6362\u94fe\u63a5\u9519\u8bef</span"
    html = (
        f'<html><body><nav><a href="{bad_href}">bad</a>'
        '<a href="/szdw/jsdw.htm">teachers</a></nav></body></html>'
    )

    links, signals = extract_links_with_signals(html, "https://sesu.scu.edu.cn/szdw/")

    assert links == ["https://sesu.scu.edu.cn/szdw/jsdw.htm"]
    assert [signal.url for signal in signals] == links


def test_buaa_teachershouw_news_query_remains_crawlable():
    url = "https://soft.buaa.edu.cn/teachershouw.jsp?urltype=news.NewsContentUrl&wbtreeid=1262&wbnewsid=9633"

    assert Fetcher.filter_same_domain([url], "https://www.buaa.edu.cn/") == [url]


def test_filter_same_domain_allows_subdomains_and_rejects_external():
    links = [
        "https://cs.pku.edu.cn/people",
        "https://www.pku.edu.cn/about",
        "https://www.baidu.com/",
    ]
    assert Fetcher.filter_same_domain(links, "https://www.pku.edu.cn/") == [
        "https://cs.pku.edu.cn/people",
        "https://www.pku.edu.cn/about",
    ]


def test_filter_same_domain_accepts_dict_links_from_llm():
    links = [
        {"url": "https://www.buaa.edu.cn/jgsz/jxkyjg02.htm"},
        {"href": "https://www.buaa.edu.cn/jgsz/dzjg01.htm"},
        {"url": "https://www.baidu.com/"},
        {"text": "missing-url"},
    ]
    assert Fetcher.filter_same_domain(links, "https://www.buaa.edu.cn/") == [
        "https://www.buaa.edu.cn/jgsz/jxkyjg02.htm",
        "https://www.buaa.edu.cn/jgsz/dzjg01.htm",
    ]


def test_is_html_content_recognizes_html_types():
    assert _is_html_content("text/html; charset=utf-8")
    assert _is_html_content("application/xhtml+xml")
    assert _is_html_content("")
    assert not _is_html_content("application/json")


def test_detect_block_reason_for_waf_challenge_status():
    html = """
    <html><body><script>
    var $_ts={};
    document.cookie='__jsl_clearance=abc';
    setTimeout(function(){},1000);
    </script></body></html>
    """
    reason = _detect_block_reason(status_code=202, body_text=html, headers=None)
    assert reason is not None
    assert "waf_challenge" in reason


def test_detect_block_reason_for_waf_like_200_page():
    html = "<html><body>" + ("challenge " * 1000) + "document.cookie setTimeout(" + "</body></html>"
    reason = _detect_block_reason(status_code=200, body_text=html, headers=None)
    assert reason is not None
    assert "waf_like_html" in reason


def test_is_ssl_error_detects_ssl_error_in_chain():
    ssl_err = ssl.SSLCertVerificationError("certificate verify failed")
    runtime_err = RuntimeError("Failed to fetch")
    runtime_err.__cause__ = ssl_err
    assert _is_ssl_error(runtime_err) is True


def test_is_ssl_error_returns_false_for_non_ssl():
    err = RuntimeError("Failed to fetch")
    err.__cause__ = RuntimeError("timed out")
    assert _is_ssl_error(err) is False
