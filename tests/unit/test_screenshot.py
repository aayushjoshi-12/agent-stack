from __future__ import annotations

import http.server
import importlib.util
import re
import struct
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_stack.artifacts.local import LocalArtifactStore
from agent_stack.tools.builtins.screenshot import check_screenshot_url, screenshot_tools
from agent_stack.tools.builtins.web import WebError

PAGE = b"""<html><head><title>Dev page</title></head>
<body style="background:#c33"><h1>Hello</h1>
<script>console.error("boom from the page"); console.log("just a log line");</script>
<script>undefinedFunction();</script></body></html>"""


@pytest.mark.parametrize(
    ("url", "address"),
    [
        ("http://localhost:3000/", "127.0.0.1"),
        ("http://dev.lan/", "192.168.1.20"),
        ("https://example.com/", "93.184.216.34"),
        ("http://[::1]:8000/", "::1"),
    ],
)
def test_local_private_and_public_hosts_are_allowed(url: str, address: str) -> None:
    assert check_screenshot_url(url, lambda host: [address]) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://[fe80::1]/",
        "http://[::ffff:169.254.169.254]/",
        "http://100.100.100.200/",
        "http://0.0.0.0:8000/",
        "file:///etc/passwd",
        "ftp://example.com/",
        "http://user:pw@localhost/",
    ],
)
def test_blocked_urls_are_refused(url: str) -> None:
    with pytest.raises(WebError):
        check_screenshot_url(url, lambda host: ["93.184.216.34"])


def test_a_name_resolving_to_metadata_is_refused() -> None:
    with pytest.raises(WebError):
        check_screenshot_url("http://metadata.google.internal/", lambda host: ["169.254.169.254"])


# ── against a real Chromium ─────────────────────────────────────────────────
def _chromium_ready() -> bool:
    if importlib.util.find_spec("playwright") is None:
        return False
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return Path(playwright.chromium.executable_path).exists()


browser = pytest.mark.skipif(
    not _chromium_ready(),
    reason="needs `uv sync --extra browser && uv run playwright install chromium`",
)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/metadata":
            self.send_response(302)
            # 0.0.0.0 is refused but still answers locally, so the test does not wait on a timeout
            self.send_header("Location", f"http://0.0.0.0:{self.server.server_address[1]}/")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.mark.integration
@browser
async def test_screenshot_saves_a_png_and_reports_console_errors(
    server: str, tmp_path: Path
) -> None:
    (web_screenshot,) = screenshot_tools(LocalArtifactStore(tmp_path))
    out = await web_screenshot.ainvoke({"url": server + "/", "width": 640, "height": 480})
    saved = re.search(r"^Saved: (\S+)", out, re.M)
    assert saved, out
    png = Path(saved.group(1)).read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (640, 480)
    assert "Title: Dev page" in out and "Status: 200" in out
    assert "boom from the page" in out and "undefinedFunction" in out
    assert "just a log line" not in out


@pytest.mark.integration
@browser
async def test_a_redirect_to_metadata_is_blocked(server: str, tmp_path: Path) -> None:
    (web_screenshot,) = screenshot_tools(LocalArtifactStore(tmp_path))
    out = await web_screenshot.ainvoke({"url": server + "/metadata"})
    assert out.startswith("Error:") and "0.0.0.0" in out, out
    assert not list(tmp_path.glob("*.png"))


async def test_a_blocked_url_is_refused_before_a_browser_starts(tmp_path: Path) -> None:
    tools = screenshot_tools(LocalArtifactStore(tmp_path))
    if not tools:
        pytest.skip("playwright not installed")
    out = await tools[0].ainvoke({"url": "file:///etc/passwd"})
    assert out == "Error: only http and https URLs are allowed"
