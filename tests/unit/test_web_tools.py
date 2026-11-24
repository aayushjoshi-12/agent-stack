from __future__ import annotations

import httpx
import pytest

from agent_stack.tools.builtins.web import (
    WebError,
    check_url,
    extract_text,
    parse_search_results,
    web_tools,
)

PAGE = """<html><head><title>Docs  page</title><style>p{}</style></head><body>
<script>alert(1)</script><h1>Intro</h1><p>Hello <b>world</b>.</p><ul><li>one</li><li>two</li></ul>
</body></html>"""

SEARCH = """<div class="result"><h2><a class="result__a"
 href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Fa&rut=x">Example  A</a></h2>
<a class="result__snippet" href="x">First <b>snippet</b></a></div>
<div class="result"><a class="result__a" href="https://duckduckgo.com/y.js?ad=1">Ad</a></div>
<div class="result"><a class="result__a" href="https://example.org/b">Example B</a>
<a class="result__snippet">Second</a></div>"""


def _public(host: str) -> list[str]:
    return ["93.184.216.34"]


def _tools(handler: httpx.MockTransport) -> dict[str, object]:
    return {t.name: t for t in web_tools(transport=handler, resolver=_public)}


def test_extract_text_drops_scripts_and_keeps_structure() -> None:
    title, text = extract_text(PAGE)
    assert title == "Docs page"
    assert "alert" not in text and "p{}" not in text
    assert "# Intro" in text and "Hello world." in text and "one\ntwo" in text


def test_search_results_unwrap_links_and_drop_ads() -> None:
    results = parse_search_results(SEARCH)
    assert [r["url"] for r in results] == ["https://example.org/a", "https://example.org/b"]
    assert results[0]["title"] == "Example A" and results[0]["snippet"] == "First snippet"


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/", "http://169.254.169.254/latest", "http://10.0.0.5/", "file:///etc/passwd",
     "http://[::1]/", "ftp://example.org/"],
)  # fmt: skip
def test_private_and_odd_urls_are_refused(url: str) -> None:
    with pytest.raises(WebError):
        check_url(url, _public)


def test_a_name_that_resolves_to_a_private_address_is_refused() -> None:
    with pytest.raises(WebError):
        check_url("http://internal.example/", lambda host: ["10.1.2.3"])


async def test_fetch_returns_labelled_text() -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})
    )
    out = await _tools(transport)["web_fetch"].ainvoke({"url": "https://example.org/x"})  # type: ignore[attr-defined]
    assert out.startswith("[Untrusted web content") and "Title: Docs page" in out
    assert "Hello world." in out


async def test_fetch_stops_a_redirect_into_a_private_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "example.org":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/secret"})
        raise AssertionError("the private address must never be requested")

    out = await _tools(httpx.MockTransport(handler))["web_fetch"].ainvoke(  # type: ignore[attr-defined]
        {"url": "https://example.org/go"}
    )
    assert out.startswith("Error:") and "private network" in out


async def test_fetch_reports_errors_and_binary_and_truncates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/missing":
            return httpx.Response(404, text="no")
        if path == "/bin":
            return httpx.Response(200, content=b"\x00\x01", headers={"content-type": "image/png"})
        return httpx.Response(200, text="x" * 5000, headers={"content-type": "text/plain"})

    tools = _tools(httpx.MockTransport(handler))
    fetch = tools["web_fetch"]
    assert "HTTP 404" in await fetch.ainvoke({"url": "https://example.org/missing"})  # type: ignore[attr-defined]
    assert "not text" in await fetch.ainvoke({"url": "https://example.org/bin"})  # type: ignore[attr-defined]
    long = await fetch.ainvoke({"url": "https://example.org/long", "max_chars": 500})  # type: ignore[attr-defined]
    assert long.endswith("…[truncated]")


async def test_search_lists_results_and_handles_failure() -> None:
    ok = httpx.MockTransport(lambda r: httpx.Response(200, text=SEARCH))
    out = await _tools(ok)["web_search"].ainvoke({"query": "agent stack"})  # type: ignore[attr-defined]
    assert "1. Example A" in out and "https://example.org/b" in out
    blocked = httpx.MockTransport(lambda r: httpx.Response(202, text=""))
    assert "HTTP 202" in await _tools(blocked)["web_search"].ainvoke({"query": "q"})  # type: ignore[attr-defined]
    assert (await _tools(ok)["web_search"].ainvoke({"query": "  "})).startswith("Error")  # type: ignore[attr-defined]
