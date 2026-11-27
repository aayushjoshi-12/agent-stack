"""P44: images reach the model, are stored by reference, replay after a restart, and every
human-readable view (transcript.md, export, search, event log) sees a placeholder, never base64."""

from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from agent_stack.app.application import Application
from agent_stack.core.images import Image, block_name, decode_image, image_block
from agent_stack.gateways.base import MessageEnvelope
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.search import ChatSearchIndex
from agent_stack.persistence.sqlite import SqliteDatabase
from tests.fakes import FakeChatModel, ai
from tests.integration.test_web_gateway import HEADERS, OWNER, _application, _client

pytestmark = pytest.mark.integration


def png(width: int = 8, height: int = 8, rgb: tuple[int, int, int] = (200, 0, 0)) -> bytes:
    """A real (tiny) PNG, built without an imaging library."""
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


RED = png()
B64 = base64.b64encode(RED).decode()


def _images_in(content: Any) -> list[bytes]:
    if not isinstance(content, list):
        return []
    return [d[0] for part in content if (d := decode_image(part))]


async def _send(application: Application, session: str, text: str, *images: Image) -> None:
    handle = await application.send_message(
        MessageEnvelope(principal=OWNER, session=session, text=text, images=images)
    )
    await handle.result()


async def test_an_image_reaches_the_model_and_is_stored_by_reference(tmp_path: Path) -> None:
    model = FakeChatModel([ai("a red square")])
    application = _application(tmp_path, model)
    session = await application.new_session(OWNER)

    await _send(application, session, "what is this?", Image(RED, "image/png", "shot.png"))

    human = model.calls[0][-1]
    assert isinstance(human, HumanMessage)
    assert _images_in(human.content) == [RED]  # an image block, then the text
    assert human.content[-1] == {"type": "text", "text": "what is this?"}

    folder = tmp_path / "sessions" / session
    name = block_name(image_block(RED, "image/png"))
    assert (folder / "artifacts" / str(name)).read_bytes() == RED
    jsonl = (folder / "transcript.jsonl").read_text()
    assert B64[:40] not in jsonl and "base64" not in jsonl
    first = json.loads(jsonl.splitlines()[0])
    assert first["content"] == "what is this?" and first["data"] == {"images": [name]}
    assert f"[image: {name}]" in (folder / "transcript.md").read_text()


async def test_the_image_is_replayed_after_a_restart(tmp_path: Path) -> None:
    model = FakeChatModel([ai("a red square"), ai("still red")])
    application = _application(tmp_path, model)
    session = await application.new_session(OWNER)
    await _send(application, session, "what is this?", Image(RED, "image/png", "shot.png"))

    fresh = FileConversationStore(tmp_path)  # a new process reading the same files
    (human, _) = await fresh.get_messages(session)
    assert human.content == model.calls[0][-1].content  # the same blocks as the first turn

    await _send(application, session, "and the colour again?")
    second = model.calls[1]
    assert [_images_in(m.content) for m in second if isinstance(m, HumanMessage)] == [[RED], []]


async def test_export_search_and_event_log_see_a_placeholder(tmp_path: Path) -> None:
    application = _application(tmp_path, FakeChatModel([ai("a red square")]))
    session = await application.new_session(OWNER)
    await _send(application, session, "look", Image(RED, "image/png", "shot.png"))
    name = str(block_name(image_block(RED, "image/png")))

    for fmt in ("md", "json"):
        text = await application.export_session(session, fmt)
        assert f"[image: {name}]" in text and B64[:40] not in text
    events = json.dumps([e.data for e in await application.replay(session_id=session)])
    assert B64[:40] not in events

    index = ChatSearchIndex(SqliteDatabase(tmp_path / "search.db"), FileConversationStore(tmp_path))
    hits = await index.search([(session, "t")], name.split(".")[0])
    assert hits and hits[0].session_id == session and B64[:20] not in hits[0].snippets[0]

    (item, _) = await application.session_history(session)
    assert item.kind == "user" and item.text == "look" and item.images == (name,)


async def test_a_branch_keeps_its_images(tmp_path: Path) -> None:
    application = _application(tmp_path, FakeChatModel([ai("a red square")]))
    session = await application.new_session(OWNER)
    await _send(application, session, "look", Image(RED, "image/png", "shot.png"))

    branch = await application.session_store.branch_before_seq(session, 99)  # type: ignore[attr-defined]
    (human, _) = await application.session_store.get_messages(branch.id)
    assert _images_in(human.content) == [RED]


async def test_a_model_without_vision_gets_the_images_as_text(tmp_path: Path) -> None:
    class TextOnly(FakeChatModel):
        async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
            if any(_images_in(m.content) for m in messages):
                self.calls.append(list(messages))
                raise RuntimeError("No endpoints found that support image input")
            return await super().ainvoke(messages)

    model = TextOnly([ai("I cannot see images; switch models")])
    application = _application(tmp_path, model)
    session = await application.new_session(OWNER)
    await _send(application, session, "what is this?", Image(RED, "image/png", "shot.png"))

    retried = model.calls[-1][-1].content
    assert len(model.calls) == 2 and not _images_in(retried)  # one refusal, one retry
    assert "does not accept images" in retried[0]["text"] and "/model" in retried[0]["text"]
    # the stored message keeps the image: a vision model later still sees it
    (human, _) = await application.session_store.get_messages(session)
    assert _images_in(human.content) == [RED]


async def test_a_tool_result_image_is_stored_and_replayed(tmp_path: Path) -> None:
    store = FileConversationStore(tmp_path)
    blocks = [image_block(RED, "image/png"), {"type": "text", "text": "Image file a.png"}]
    await store.append_messages("s", [ToolMessage(content=blocks, tool_call_id="t1")])

    assert B64[:40] not in (tmp_path / "sessions" / "s" / "transcript.jsonl").read_text()
    (tool,) = await FileConversationStore(tmp_path).get_messages("s")
    assert tool.content == blocks


async def test_web_upload_history_and_image_route(tmp_path: Path) -> None:
    model = FakeChatModel([ai("a red square")])
    application = _application(tmp_path, model)
    name = str(block_name(image_block(RED, "image/png")))
    async for client in _client(application):
        session = (await client.post("/sessions", headers=HEADERS)).json()["id"]
        bad = await client.post(
            f"/sessions/{session}/messages",
            headers=HEADERS,
            json={
                "images": [{"name": "x.png", "data": base64.b64encode(b"not an image").decode()}]
            },
        )
        assert bad.status_code == 400 and "not a PNG" in bad.json()["detail"]
        assert (
            await client.post(f"/sessions/{session}/messages", headers=HEADERS, json={"text": ""})
        ).status_code == 422

        sent = await client.post(
            f"/sessions/{session}/messages",
            headers=HEADERS,
            json={"text": "", "images": [{"name": "shot.png", "data": B64}]},
        )
        assert sent.status_code == 202
        await client.get(f"/runs/{sent.json()['run_id']}/events", headers=HEADERS)  # to the end
        assert _images_in(model.calls[0][-1].content) == [RED]

        history = (await client.get(f"/sessions/{session}", headers=HEADERS)).json()
        assert history["title"] == "shot.png"
        assert history["history"][0]["images"] == [name]

        got = await client.get(f"/sessions/{session}/images/{name}", headers=HEADERS)
        assert got.status_code == 200 and got.content == RED
        assert got.headers["content-type"] == "image/png"
        for wrong in ("../session.json", "transcript.jsonl", "0" * 16 + ".png"):
            missing = await client.get(f"/sessions/{session}/images/{wrong}", headers=HEADERS)
            assert missing.status_code == 404
        assert (await client.get(f"/sessions/{session}/images/{name}")).status_code == 401
