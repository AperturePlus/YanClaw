from __future__ import annotations

import ssl
import time

import httpx
import pytest

from agents.crawler.fetcher import Fetcher, _is_ssl_error


async def test_fetcher_converts_html_extracts_links_and_rate_limits():
    async def handler(request):
        return httpx.Response(
            200,
            text='<html><body><a href="/faculty">Faculty</a><p>Hello</p></body></html>',
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
        )

    fetcher = Fetcher(
        request_interval_seconds=0.05,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    async with fetcher:
        start = time.monotonic()
        first = await fetcher.fetch("https://www.example.edu.cn/")
        second = await fetcher.fetch("https://www.example.edu.cn/next")
        elapsed = time.monotonic() - start

    assert "Hello" in first.text
    assert "https://www.example.edu.cn/faculty" in first.links
    assert second.status_code == 200
    assert elapsed >= 0.005  # rate limit enforced (relaxed for Windows timer resolution)


async def test_fetcher_retries_429_then_succeeds():
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, text="slow down", request=request)
        return httpx.Response(200, text="<p>ok</p>", request=request)

    fetcher = Fetcher(
        request_interval_seconds=0,
        max_retries=1,
        retry_base_delay=0,
        transport=httpx.MockTransport(handler),
    )
    async with fetcher:
        result = await fetcher.fetch("https://www.example.edu.cn/")
    assert result.status_code == 200
    assert calls == 2


async def test_fetcher_marks_202_waf_challenge_pages():
    async def handler(request):
        html = """
        <html><body><script>
        var $_ts={};
        document.cookie='__jsl_clearance=abc';
        setTimeout(function(){},1000);
        </script></body></html>
        """
        return httpx.Response(
            202,
            text=html,
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
        )

    fetcher = Fetcher(
        request_interval_seconds=0,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    async with fetcher:
        result = await fetcher.fetch("https://www.example.edu.cn/")

    assert result.status_code == 202
    assert result.block_reason is not None
    assert "waf_challenge" in result.block_reason


async def test_fetcher_marks_200_waf_like_html_pages():
    async def handler(request):
        html = "<html><body>" + ("challenge " * 1000) + "document.cookie setTimeout(" + "</body></html>"
        return httpx.Response(
            200,
            text=html,
            request=request,
            headers={"content-type": "text/html; charset=utf-8"},
        )

    fetcher = Fetcher(
        request_interval_seconds=0,
        max_retries=0,
        transport=httpx.MockTransport(handler),
    )
    async with fetcher:
        result = await fetcher.fetch("https://www.example.edu.cn/")

    assert result.status_code == 200
    assert result.block_reason is not None
    assert "waf_like_html" in result.block_reason


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



# --- SSL fallback tests ---


def _ssl_then_ok_transport() -> tuple[httpx.MockTransport, httpx.MockTransport]:
    """Return (failing_transport, insecure_transport) for SSL fallback tests."""

    def ssl_handler(request: httpx.Request) -> httpx.Response:
        ssl_err = ssl.SSLCertVerificationError("certificate verify failed")
        raise httpx.ConnectError(str(ssl_err)) from ssl_err

    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body><p>OK</p></body></html>",
            request=request,
            headers={"content-type": "text/html"},
        )

    return httpx.MockTransport(ssl_handler), httpx.MockTransport(ok_handler)


async def test_fetcher_falls_back_to_insecure_client_on_ssl_error():
    failing_transport, ok_transport = _ssl_then_ok_transport()

    fetcher = Fetcher(
        request_interval_seconds=0,
        max_retries=0,
        retry_base_delay=0,
        transport=failing_transport,
    )
    async with fetcher:
        # Inject the insecure client backed by the ok_transport
        fetcher._insecure_client = httpx.AsyncClient(
            transport=ok_transport, follow_redirects=True
        )
        result = await fetcher.fetch("https://math.pku.edu.cn/")

    assert result.status_code == 200
    assert "OK" in result.text


async def test_fetcher_raises_non_ssl_errors_without_fallback():
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    fetcher = Fetcher(
        request_interval_seconds=0,
        max_retries=0,
        retry_base_delay=0,
        transport=httpx.MockTransport(timeout_handler),
    )
    async with fetcher:
        with pytest.raises(RuntimeError, match="Failed to fetch"):
            await fetcher.fetch("https://example.edu.cn/")
    # Insecure client should never have been created
    assert fetcher._insecure_client is None


async def test_fetcher_closes_insecure_client_on_exit():
    failing_transport, ok_transport = _ssl_then_ok_transport()

    fetcher = Fetcher(
        request_interval_seconds=0,
        max_retries=0,
        retry_base_delay=0,
        transport=failing_transport,
    )
    async with fetcher:
        fetcher._insecure_client = httpx.AsyncClient(
            transport=ok_transport, follow_redirects=True
        )
        await fetcher.fetch("https://math.pku.edu.cn/")

    assert fetcher._insecure_client.is_closed


# --- _is_ssl_error unit tests ---


def test_is_ssl_error_detects_ssl_error_in_chain():
    ssl_err = ssl.SSLCertVerificationError("certificate verify failed")
    connect_err = httpx.ConnectError(str(ssl_err))
    connect_err.__cause__ = ssl_err
    runtime_err = RuntimeError("Failed to fetch")
    runtime_err.__cause__ = connect_err
    assert _is_ssl_error(runtime_err) is True


def test_is_ssl_error_returns_false_for_non_ssl():
    err = RuntimeError("Failed to fetch")
    err.__cause__ = httpx.ReadTimeout("timed out")
    assert _is_ssl_error(err) is False


async def test_fetcher_ssl_handshake_failure_skips_retries_and_falls_back():
    """SSL handshake failures (SECLEVEL mismatch) should not waste retries
    and should fall back to the insecure client with relaxed ciphers."""
    primary_calls = 0

    def ssl_handshake_handler(request: httpx.Request) -> httpx.Response:
        nonlocal primary_calls
        primary_calls += 1
        raise httpx.ConnectError(
            "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] ssl/tls alert handshake failure"
        )

    def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><body><p>OK</p></body></html>",
            request=request,
            headers={"content-type": "text/html"},
        )

    fetcher = Fetcher(
        request_interval_seconds=0,
        max_retries=3,
        retry_base_delay=0,
        transport=httpx.MockTransport(ssl_handshake_handler),
    )
    async with fetcher:
        fetcher._insecure_client = httpx.AsyncClient(
            transport=httpx.MockTransport(ok_handler), follow_redirects=True
        )
        result = await fetcher.fetch("https://www.gsm.pku.edu.cn/")

    assert result.status_code == 200
    assert "OK" in result.text
    # SSL error is deterministic — should break after 1 attempt, not 4
    assert primary_calls == 1
