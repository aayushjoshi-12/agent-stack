"""The web API's history endpoints, on the real application."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agent_stack.gateways.base import MessageEnvelope
from agent_stack.gateways.web import WebSettings, create_app
from tests.fakes import FakeChatModel, ai, call
from tests.integration.test_changes import World, _world

pytestmark = pytest.mark.integration

TOKEN = "t" * 24
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


async def _setup(tmp_path: Path) -> tuple[httpx.AsyncClient, World]:
    model = FakeChatModel(
        [
            ai(tool_calls=[call("write_file", {"path": "a.txt", "content": "v1\n"})]),
            ai("wrote a"),
            ai(tool_calls=[call("write_file", {"path": "a.txt", "content": "v2\n"})]),
            ai("changed a"),
        ]
    )
    w = _world(tmp_path, model)
    for text in ("make a", "change a"):
        handle = await w.app.send_message(MessageEnvelope(principal="u1", session="s1", text=text))
        await handle.result()
    settings = WebSettings(token=TOKEN, principal="u1")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(w.app, settings)), base_url="http://t"
    )
    return client, w


async def test_turns_rewind_tree_and_file_endpoints(tmp_path: Path) -> None:
    client, w = await _setup(tmp_path)
    async with client:
        turns = (await client.get("/sessions/s1/turns", headers=HEADERS)).json()
        assert [t["user_text"] for t in turns] == ["make a", "change a"]
        assert [t["files_changed"] for t in turns] == [1, 1]

        target = turns[1]
        body = {
            "owner_session_id": target["owner_session_id"],
            "started_event_id": target["started_event_id"],
        }
        done = await client.post("/sessions/s1/rewind", json=body, headers=HEADERS)
        assert done.status_code == 200
        result = done.json()
        assert result["prompt"] == "change a" and result["session_id"] != "s1"
        assert result["files"]["moved"] == [{"path": "a.txt", "action": "wrote"}]
        assert (w.workspace / "a.txt").read_text() == "v1\n"

        fork = result["session_id"]
        tree = (await client.get(f"/sessions/{fork}/tree", headers=HEADERS)).json()
        assert tree["mermaid"].startswith("flowchart TD") and "s1" in tree["text"]

        status_ = (await client.get("/sessions/s1/files/status", headers=HEADERS)).json()
        assert status_ == {"differs": True}  # the folder holds the fork's files
        restored = (await client.post("/sessions/s1/files/restore", headers=HEADERS)).json()
        assert restored["moved"] == [{"path": "a.txt", "action": "wrote"}]
        assert (w.workspace / "a.txt").read_text() == "v2\n"
        assert (await client.get("/sessions/s1/files/status", headers=HEADERS)).json() == {
            "differs": False
        }


async def test_references_make_describe_open_and_are_owner_scoped(tmp_path: Path) -> None:
    client, _ = await _setup(tmp_path)
    async with client:
        made = await client.post("/sessions/s1/reference", json={}, headers=HEADERS)
        ref = made.json()["reference"]
        assert ref.startswith("asref://")
        text = (
            await client.post("/references/describe", json={"reference": ref}, headers=HEADERS)
        ).json()["text"]
        assert "USER: make a" in text and "a.txt" in text
        opened = await client.post("/references/open", json={"reference": ref}, headers=HEADERS)
        assert opened.status_code == 201 and opened.json()["id"] != "s1"

        assert (
            await client.post("/references/open", json={"reference": "nope"}, headers=HEADERS)
        ).status_code == 400
        stranger = ref.replace("asref://s1", "asref://other")
        assert (
            await client.post("/references/open", json={"reference": stranger}, headers=HEADERS)
        ).status_code == 404  # not a session you own


async def test_history_endpoints_need_the_token_and_a_real_turn(tmp_path: Path) -> None:
    client, _ = await _setup(tmp_path)
    async with client:
        assert (await client.get("/sessions/s1/turns")).status_code == 401
        bad = {"owner_session_id": "s1", "started_event_id": "nope"}
        r = await client.post("/sessions/s1/rewind", json=bad, headers=HEADERS)
        assert r.status_code == 404


async def test_commands_and_approval_mode_endpoints(tmp_path: Path) -> None:
    from agent_stack.core.approval import ApprovalMode
    from agent_stack.skills import SkillLoader, SkillRegistry

    client, w = await _setup(tmp_path)
    folder = tmp_path / "skills" / "release-notes"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: release-notes\ndescription: Draft release notes.\n---\nGroup the changes.\n"
    )
    w.app.skills = SkillRegistry(SkillLoader([tmp_path / "skills"]))
    w.app.approval_mode = ApprovalMode("ask")
    async with client:
        listing = (await client.get("/commands", headers=HEADERS)).json()
        assert listing == [
            {"name": "release-notes", "description": "Draft release notes.", "kind": "skill"}
        ]
        assert (await client.get("/approval-mode", headers=HEADERS)).json() == {
            "mode": "ask",
            "modes": ["ask", "auto"],
        }
        put = await client.put("/approval-mode", json={"mode": "auto"}, headers=HEADERS)
        assert put.json() == {"mode": "auto"} and w.app.get_approval_mode() == "auto"
        bad = await client.put("/approval-mode", json={"mode": "yolo"}, headers=HEADERS)
        assert bad.status_code == 400
        assert (await client.get("/commands")).status_code == 401


async def test_model_endpoint_switches_the_model_for_the_next_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent_stack.core import factory as factory_module

    client, w = await _setup(tmp_path)
    real_build = factory_module.build_model
    switched = FakeChatModel([ai("answered by the new model")])
    monkeypatch.setattr(
        factory_module,
        "build_model",
        lambda name: switched if name == "test:other" else real_build(name),
    )
    async with client:
        assert (await client.get("/model", headers=HEADERS)).json() == {"model": "test:model"}
        put = await client.put("/model", json={"model": "test:other"}, headers=HEADERS)
        assert put.json() == {"model": "test:other"} and w.app.get_model() == "test:other"
        bad = await client.put("/model", json={"model": "no-such-provider"}, headers=HEADERS)
        assert bad.status_code == 400 and w.app.get_model() == "test:other"
        assert (await client.get("/model")).status_code == 401
    handle = await w.app.send_message(MessageEnvelope(principal="u1", session="s1", text="hi"))
    reply = await handle.result()
    assert reply.content == "answered by the new model"


async def test_compact_endpoint_passes_the_focus_for_owned_sessions(tmp_path: Path) -> None:
    client, w = await _setup(tmp_path)
    async with client:
        assert (
            await client.post("/sessions/nope/compact", json={}, headers=HEADERS)
        ).status_code == 404

        seen: list[tuple[str, str | None]] = []

        async def fake_compact(session_id: str, focus: str | None = None) -> int:
            seen.append((session_id, focus))
            return 4

        w.app.compact_session = fake_compact  # type: ignore[method-assign]
        done = await client.post("/sessions/s1/compact", json={"focus": "files"}, headers=HEADERS)
        assert done.json() == {"messages": 4} and seen == [("s1", "files")]
