"""P41 ``/export``: file naming, argument parsing, and each gateway's command."""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console

from agent_stack.app.export import export_filename, free_path, parse_export_args
from agent_stack.gateways.base import SessionSummary
from agent_stack.gateways.cli import run_cli
from agent_stack.gateways.telegram import TelegramApi
from agent_stack.gateways.tui.app import TuiApp
from agent_stack.gateways.tui.chrome import PromptArea
from tests.unit.gateway_fakes import FakeApplication
from tests.unit.test_telegram_gateway import TOKEN, FakeApi, _gateway, _say


def test_file_name_is_a_slug_of_the_title_and_the_date() -> None:
    day = date(2026, 11, 27)
    assert export_filename("Fix the login bug!", "md", day) == "fix-the-login-bug-2026-11-27.md"
    assert export_filename(None, "json", day) == "conversation-2026-11-27.json"
    assert export_filename("???", "md", day) == "conversation-2026-11-27.md"


def test_an_existing_file_is_never_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "chat.md"
    assert free_path(target) == target
    target.write_text("old")
    (tmp_path / "chat-2.md").write_text("older")
    assert free_path(target) == tmp_path / "chat-3.md"


def test_export_arguments() -> None:
    assert parse_export_args("") == ("md", None)
    assert parse_export_args("json") == ("json", None)
    assert parse_export_args("JSON out/chat.json") == ("json", "out/chat.json")
    assert parse_export_args("notes/today.md") == ("md", "notes/today.md")
    assert parse_export_args("dump.json") == ("json", "dump.json")


# ── TUI ──────────────────────────────────────────────────────────────────────
async def test_tui_export_writes_and_notifies_even_while_a_run_is_going() -> None:
    fake = FakeApplication()
    app = TuiApp(fake, session_id="s1")  # type: ignore[arg-type]
    notes: list[str] = []
    async with app.run_test(size=(120, 40)) as pilot:
        app.notify = lambda message, **kw: notes.append(str(message))  # type: ignore[method-assign]
        app.tab.running = True
        prompt = app.query_one("#prompt", PromptArea)
        prompt.load_text("/export json logs/x.json")
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
    assert fake.exports == [("s1", "json", "logs/x.json")]
    assert fake.inputs == [] and fake.sent == []  # not queued into the run, not a message
    assert any("Exported to" in n and "x.json" in n for n in notes)
    assert "export" in {name for name, _, _ in app.command_entries()}


# ── line REPL ────────────────────────────────────────────────────────────────
async def test_cli_export_defaults_to_markdown_and_prints_the_path() -> None:
    fake = FakeApplication()
    buffer = io.StringIO()
    console = Console(file=buffer, no_color=True, width=200, highlight=False)
    await run_cli(fake, instream=io.StringIO("/export\n/export json\nexit\n"), out=console)  # type: ignore[arg-type]
    session = fake.exports[0][0]
    assert fake.exports == [(session, "md", None), (session, "json", None)]
    assert "exported to" in buffer.getvalue() and "chat-2026-11-27.json" in buffer.getvalue()
    assert fake.sent == []


# ── Telegram ─────────────────────────────────────────────────────────────────
class DocumentApi(FakeApi):
    def __init__(self) -> None:
        super().__init__()
        self.documents: list[tuple[int, str, bytes]] = []

    async def send_document(
        self, chat_id: int, filename: str, content: bytes, caption: str | None = None
    ) -> None:
        self.documents.append((chat_id, filename, content))


async def test_telegram_export_sends_the_conversation_as_a_document() -> None:
    fake = FakeApplication()
    gateway, api = _gateway(fake, DocumentApi())
    await _say(gateway, "/export")
    assert any("Nothing to export yet" in t for t in api.texts())
    fake.sessions = [SessionSummary(id="s9", title="Chat")]
    gateway._chats[7].session = "s9"
    await _say(gateway, "/export")
    await _say(gateway, "/export json")
    await _say(gateway, "/export pdf")
    assert api.documents == [
        (7, "chat-2026-11-27.md", b"export of s9 as md"),
        (7, "chat-2026-11-27.json", b"export of s9 as json"),
    ]
    assert any("Usage: /export [md|json]" in t for t in api.texts())


async def test_send_document_uploads_a_multipart_file() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.telegram.org"
    )
    api = TelegramApi(TOKEN, client)
    await api.send_document(7, "chat.md", b"# hello")
    request: Any = seen[0]
    body = request.content
    assert request.url.path.endswith("/sendDocument")
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b'filename="chat.md"' in body and b"# hello" in body and b'name="chat_id"' in body
    await api.aclose()
