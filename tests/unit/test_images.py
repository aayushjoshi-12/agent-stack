"""P44: the image helpers, image tool results, and each gateway's way of attaching an image."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from rich.console import Console

from agent_stack.core.context import estimate_tokens
from agent_stack.core.images import (
    IMAGE_TOKENS,
    MAX_IMAGE_BYTES,
    content_text,
    decode_image,
    image_block,
    make_image,
)
from agent_stack.core.workspace import WorkspacePolicy
from agent_stack.gateways.base import image_paths
from agent_stack.gateways.cli import run_cli
from agent_stack.gateways.telegram import TelegramError
from agent_stack.gateways.tui.app import TuiApp
from agent_stack.gateways.tui.chrome import PromptArea
from agent_stack.gateways.tui.rows import UserMessage
from agent_stack.tools.builtins.filesystem import filesystem_tools
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.mcp import _image_blocks
from agent_stack.tools.registry import ToolRegistry
from tests.integration.test_images import RED, png
from tests.unit.gateway_fakes import FakeApplication
from tests.unit.test_telegram_gateway import OWNER, FakeApi, _gateway, _settle


def test_make_image_sniffs_the_type_and_caps_the_size() -> None:
    assert make_image(RED, "a.png").mime == "image/png"
    assert make_image(b"\xff\xd8\xff\xe0rest", "b.jpg").mime == "image/jpeg"
    assert make_image(b"RIFF\0\0\0\0WEBPVP8 ", "c.webp").mime == "image/webp"
    with pytest.raises(ValueError, match="not a PNG"):
        make_image(b"%PDF-1.7", "d.png")  # the name does not decide the type
    with pytest.raises(ValueError, match="limited to 5 MB"):
        make_image(RED + b"\0" * MAX_IMAGE_BYTES, "e.png")


def test_the_token_estimate_counts_an_image_not_its_base64() -> None:
    big = png(400, 400)
    message = HumanMessage(content=[image_block(big, "image/png"), {"type": "text", "text": "hi"}])
    assert estimate_tokens([message]) == IMAGE_TOKENS + 0  # "hi" rounds away
    assert content_text(message.content) == "hi"


def test_image_paths_finds_at_paths_and_pasted_paths(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(RED)
    (tmp_path / "My Shot.png").write_bytes(RED)
    (tmp_path / "notes.txt").write_text("x")

    rest, found = image_paths("describe @shot.png please", tmp_path)
    assert rest == "describe please" and found == [tmp_path / "shot.png"]

    pasted = str(tmp_path / "My Shot.png").replace(" ", "\\ ")
    rest, found = image_paths(f"{pasted} what is it", tmp_path)
    assert rest == "what is it" and found == [tmp_path / "My Shot.png"]
    rest, found = image_paths(f"'{tmp_path / 'My Shot.png'}'", tmp_path)
    assert rest == "" and found == [tmp_path / "My Shot.png"]

    for kept in ("rename shot.png to b.png", "@notes.txt", "@missing.png", "@explorer find it"):
        assert image_paths(kept, tmp_path) == (kept, [])


async def test_read_file_on_an_image_returns_an_image_block(tmp_path: Path) -> None:
    (tmp_path / "pic.png").write_bytes(RED)
    (tmp_path / "fake.png").write_text("not an image")
    executor = ToolExecutor(ToolRegistry(filesystem_tools(WorkspacePolicy(tmp_path))))

    result = await executor.execute_one(
        {"id": "1", "name": "read_file", "args": {"path": "pic.png"}}
    )
    image, text = result.message.content
    assert decode_image(image) == (RED, "image/png") and "Image file pic.png" in text["text"]

    bad = await executor.execute_one({"id": "2", "name": "read_file", "args": {"path": "fake.png"}})
    assert not bad.success and "not a PNG" in str(bad.message.content)


def test_mcp_image_content_becomes_an_image_block() -> None:
    result = SimpleNamespace(
        content=[
            SimpleNamespace(
                type="image", data=base64.b64encode(RED).decode(), mimeType="image/png"
            ),
            SimpleNamespace(type="image", data="bm90IGFuIGltYWdl", mimeType="image/png"),
            SimpleNamespace(type="text", text="done"),
        ]
    )
    assert _image_blocks(result) == [image_block(RED, "image/png")]  # the bad one is skipped


# ── gateways ─────────────────────────────────────────────────────────────────
async def test_tui_shows_a_chip_and_sends_the_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shot.png").write_bytes(RED)
    application = FakeApplication()
    app = TuiApp(application, session_id="s1")  # type: ignore[arg-type]

    async with app.run_test(size=(140, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        prompt.load_text("what is in @shot.png")
        await pilot.pause()
        chips = app.query_one("#attachments")
        assert chips.display and "image: shot.png" in str(chips.render())
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()
        row = app.tab.query(UserMessage).first()
        assert "[image: shot.png]" in str(row.render())

    envelope = application.sent[0][0]
    assert envelope.text == "what is in" and [i.name for i in envelope.images] == ["shot.png"]
    assert envelope.images[0].data == RED


async def test_tui_image_command_puts_the_path_in_the_prompt() -> None:
    app = TuiApp(FakeApplication(), session_id="s1")  # type: ignore[arg-type]
    async with app.run_test(size=(140, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        prompt.load_text("/image ~/My Pics/a.png")
        await pilot.press("enter")
        await pilot.pause()
        assert prompt.text == "@'~/My Pics/a.png' "


async def test_line_repl_attaches_at_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shot.png").write_bytes(RED)
    application = FakeApplication()
    out = io.StringIO()
    console = Console(file=out, no_color=True, width=120)
    await run_cli(application, instream=io.StringIO("@shot.png what is it\nexit\n"), out=console)  # type: ignore[arg-type]

    envelope = application.sent[0][0]
    assert envelope.text == "what is it" and envelope.images[0].data == RED
    assert "[image: shot.png]" in out.getvalue()


class PhotoApi(FakeApi):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__()
        self.files = files
        self.downloads: list[str] = []

    async def download_file(self, file_id: str, max_bytes: int) -> bytes:
        self.downloads.append(file_id)
        if file_id not in self.files:
            raise TelegramError("file not found")
        return self.files[file_id]


def _photo_update(update_id: int, **message: Any) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": OWNER},
            "chat": {"id": OWNER, "type": "private"},
            **message,
        },
    }


async def test_telegram_photo_and_image_document_are_sent_with_the_caption() -> None:
    application = FakeApplication()
    gateway, api = _gateway(application, PhotoApi({"big": RED, "doc": RED}))
    photo = [{"file_id": "small", "width": 90}, {"file_id": "big", "width": 1280}]

    await gateway.handle_update(_photo_update(70001, photo=photo, caption="what is this?"))
    await _settle(gateway)
    document = {"file_id": "doc", "mime_type": "image/png", "file_name": "chart.png"}
    await gateway.handle_update(_photo_update(70002, document=document))
    await _settle(gateway)

    assert api.downloads == ["big", "doc"]  # the largest size of the photo
    first, second = (sent[0] for sent in application.sent)
    assert first.text == "what is this?" and first.images[0].data == RED
    assert second.text == "" and second.images[0].name == "chart.png"


async def test_telegram_refuses_a_bad_image_and_an_image_during_a_run() -> None:
    application = FakeApplication()
    gateway, api = _gateway(application, PhotoApi({"pdf": b"%PDF-1.7"}))
    await gateway.handle_update(_photo_update(70003, photo=[{"file_id": "pdf"}]))
    assert "Cannot use that image" in api.texts()[-1] and not application.sent

    chat = gateway._chat(OWNER, OWNER)
    chat.task = SimpleNamespace(done=lambda: False)  # type: ignore[assignment]
    await gateway.handle_update(_photo_update(70004, photo=[{"file_id": "pdf"}]))
    assert "Send the image again when it is done" in api.texts()[-1]
