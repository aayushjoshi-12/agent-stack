"""P24 web gateway: auth, ownership, resumable SSE, and a stop that really stops spending."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessageChunk

from agent_stack.app.application import Application
from agent_stack.app.config import AppConfig
from agent_stack.core.agent import Agent, SubAgentSpec
from agent_stack.core.approval import ApprovalBroker
from agent_stack.core.factory import AgentFactory
from agent_stack.core.run import RunManager
from agent_stack.core.runtime import AgentRuntime
from agent_stack.gateways.web import WebConfigError, WebSettings, create_app
from agent_stack.observability.metrics import Metrics
from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.event_log import EventStoreBus, SqliteEventLogStore
from agent_stack.persistence.memory import InMemoryRunStore
from agent_stack.persistence.sqlite import SqliteDatabase, SqliteSessionStore
from agent_stack.subagents.delegation import delegate
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, FakeStreamingModel, SleepTool, ai, call

pytestmark = pytest.mark.integration

TOKEN = "t" * 24
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
OWNER = "tui:local-user"


class BlockingModel:
    """A model call that never returns on its own; records whether it was interrupted."""

    def __init__(self) -> None:
        self.calls = 0
        self.interrupted = 0
        self.started = asyncio.Event()

    def bind_tools(self, tools: Sequence[Any]) -> BlockingModel:
        return self

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.interrupted += 1
            raise


class _Factory:
    def __init__(self, runtimes: dict[str, AgentRuntime]) -> None:
        self._runtimes = runtimes

    def create(self, agent: Agent) -> AgentRuntime:
        return self._runtimes[agent.name]


def _application(
    tmp_path: Path, model: Any, *, tools: Sequence[Any] = (), child_model: Any = None
) -> Application:
    db = SqliteDatabase(tmp_path / "index.db")
    log = SqliteEventLogStore(db)
    store = SqliteSessionStore(db, FileConversationStore(tmp_path), log)
    bus = EventStoreBus(log)
    subagents: tuple[SubAgentSpec, ...] = ()
    runtimes: dict[str, AgentRuntime] = {}
    all_tools = list(tools)
    if child_model is not None:
        explorer = Agent(name="explorer", model="test:model")
        subagents = (SubAgentSpec("explorer", "Explores.", explorer),)
        runtimes["explorer"] = AgentRuntime(
            agent=explorer,
            model=child_model,
            tool_executor=ToolExecutor(ToolRegistry([])),
            session_store=store,
            event_bus=bus,
        )
        all_tools.append(delegate)
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tuple(all_tools),  # type: ignore[arg-type]
        system_prompt="t",
        subagents=subagents,
    )
    runtime = AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(ToolRegistry(all_tools)),  # type: ignore[arg-type]
        session_store=store,
        event_bus=bus,
    )
    manager = RunManager(
        runtime=runtime,
        run_store=InMemoryRunStore(),
        event_bus=bus,
        agent_factory=_Factory(runtimes),  # type: ignore[arg-type]
    )
    return Application(
        config=AppConfig(),
        agent=agent,
        factory=AgentFactory(session_store=store, event_bus=bus),
        session_store=store,
        run_manager=manager,
        event_bus=bus,
        approvals=ApprovalBroker(),
        metrics=Metrics(),
        event_log=log,
    )


async def _client(application: Application, **settings: Any) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(application, WebSettings(token=TOKEN, **settings))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture
async def web(tmp_path: Path) -> AsyncIterator[tuple[httpx.AsyncClient, Application]]:
    application = _application(tmp_path, FakeChatModel([ai("hello there")]))
    async for client in _client(application):
        yield client, application


def _events(body: str) -> list[dict[str, Any]]:
    out = []
    for block in body.split("\n\n"):
        lines = {
            k: v for k, _, v in (ln.partition(": ") for ln in block.splitlines() if ": " in ln)
        }
        if "data" in lines:
            out.append({"sse_id": lines.get("id"), **json.loads(lines["data"])})
    return out


async def _start(client: httpx.AsyncClient, text: str = "hi") -> tuple[str, str]:
    session = (await client.post("/sessions", headers=HEADERS)).json()["id"]
    response = await client.post(
        f"/sessions/{session}/messages", headers=HEADERS, json={"text": text}
    )
    assert response.status_code == 202
    return session, response.json()["run_id"]


# ── settings: fail closed ────────────────────────────────────────────────────
def test_settings_require_a_real_token_and_hide_it() -> None:
    for env in ({}, {"AGENT_STACK_WEB_TOKEN": "short"}):
        with pytest.raises(WebConfigError, match="AGENT_STACK_WEB_TOKEN"):
            WebSettings.from_env(env)
    settings = WebSettings.from_env(
        {"AGENT_STACK_WEB_TOKEN": TOKEN, "AGENT_STACK_WEB_PORT": "9000"}
    )
    assert settings.host == "127.0.0.1" and settings.port == 9000
    assert TOKEN not in repr(settings)
    with pytest.raises(WebConfigError, match="PORT"):
        WebSettings.from_env({"AGENT_STACK_WEB_TOKEN": TOKEN, "AGENT_STACK_WEB_PORT": "x"})


async def test_every_route_but_health_needs_the_token(
    web: tuple[httpx.AsyncClient, Application],
) -> None:
    client, _ = web
    assert (await client.get("/health")).status_code == 200
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Basic {TOKEN}"}):
        assert (await client.get("/sessions", headers=headers)).status_code == 401
        assert (await client.post("/sessions", headers=headers)).status_code == 401
        assert (await client.post("/runs/r/cancel", headers=headers)).status_code == 401
        assert (await client.get("/runs/r/events", headers=headers)).status_code == 401
    assert (await client.get("/sessions", headers=HEADERS)).status_code == 200


# ── sessions and ownership ───────────────────────────────────────────────────
async def test_session_lifecycle_and_history(web: tuple[httpx.AsyncClient, Application]) -> None:
    client, _ = web
    session, run_id = await _start(client, "say hello")
    await client.get(f"/runs/{run_id}/events", headers=HEADERS)  # drains until the run ends

    listed = (await client.get("/sessions", headers=HEADERS)).json()
    assert [s["id"] for s in listed] == [session]

    detail = (await client.get(f"/sessions/{session}", headers=HEADERS)).json()
    assert [(h["kind"], h["text"]) for h in detail["history"]] == [
        ("user", "say hello"),
        ("assistant", "hello there"),
    ]

    renamed = await client.patch(
        f"/sessions/{session}", headers=HEADERS, json={"title": "Greeting"}
    )
    assert renamed.json()["title"] == "Greeting"
    assert (await client.delete(f"/sessions/{session}", headers=HEADERS)).status_code == 204
    assert (await client.get(f"/sessions/{session}", headers=HEADERS)).status_code == 404


async def test_another_principals_session_and_run_look_nonexistent(
    web: tuple[httpx.AsyncClient, Application],
) -> None:
    client, application = web
    foreign = await application.new_session("telegram:999")
    handle = await application.send_message(
        __import__("agent_stack.gateways.base", fromlist=["MessageEnvelope"]).MessageEnvelope(
            principal="telegram:999", session=foreign, text="secret"
        )
    )
    await handle.result()

    for method, path in (
        ("GET", f"/sessions/{foreign}"),
        ("GET", f"/sessions/{foreign}/events"),
        ("DELETE", f"/sessions/{foreign}"),
        ("GET", f"/runs/{handle.run_id}"),
        ("GET", f"/runs/{handle.run_id}/events"),
        ("POST", f"/runs/{handle.run_id}/cancel"),
    ):
        response = await client.request(method, path, headers=HEADERS)
        assert response.status_code == 404, (method, path)
    posted = await client.post(f"/sessions/{foreign}/messages", headers=HEADERS, json={"text": "x"})
    assert posted.status_code == 404
    assert (await client.get("/sessions", headers=HEADERS)).json() == []


async def test_the_first_message_titles_the_conversation_and_later_ones_do_not(
    web: tuple[httpx.AsyncClient, Application],
) -> None:
    client, _ = web
    session, run_id = await _start(client, "  fix the retry bug  ")
    await client.get(f"/runs/{run_id}/events", headers=HEADERS)
    again = await client.post(
        f"/sessions/{session}/messages", headers=HEADERS, json={"text": "other"}
    )
    assert again.status_code in (
        202,
        500,
    )  # the scripted model has one reply; only the title matters

    listed = (await client.get("/sessions", headers=HEADERS)).json()

    assert [s["title"] for s in listed] == ["fix the retry bug"]


async def test_message_validation(web: tuple[httpx.AsyncClient, Application]) -> None:
    client, _ = web
    session = (await client.post("/sessions", headers=HEADERS)).json()["id"]
    for body in ({"text": ""}, {}, {"text": "x" * 100_001}):
        response = await client.post(f"/sessions/{session}/messages", headers=HEADERS, json=body)
        assert response.status_code == 422


# ── the SSE stream ───────────────────────────────────────────────────────────
async def test_the_stream_delivers_the_run_in_order_and_ends(
    web: tuple[httpx.AsyncClient, Application],
) -> None:
    client, _ = web
    _, run_id = await _start(client)

    response = await client.get(f"/runs/{run_id}/events", headers=HEADERS)

    assert response.headers["content-type"].startswith("text/event-stream")
    events = _events(response.text)
    assert [e["type"] for e in events] == ["run.started", "assistant.message", "run.completed"]
    assert [e["sequence"] for e in events] == sorted(e["sequence"] for e in events)
    assert [str(e["sequence"]) for e in events] == [e["sse_id"] for e in events]
    assert events[1]["data"]["content"] == "hello there"


async def test_a_reconnect_resumes_after_the_last_received_event(
    web: tuple[httpx.AsyncClient, Application],
) -> None:
    client, _ = web
    _, run_id = await _start(client)
    full = _events((await client.get(f"/runs/{run_id}/events", headers=HEADERS)).text)
    first = full[0]["sequence"]

    by_header = await client.get(
        f"/runs/{run_id}/events", headers={**HEADERS, "Last-Event-ID": str(first)}
    )
    by_query = await client.get(
        f"/runs/{run_id}/events?last_event_sequence={first}", headers=HEADERS
    )

    expected = [e["id"] for e in full[1:]]
    assert [e["id"] for e in _events(by_header.text)] == expected
    assert [e["id"] for e in _events(by_query.text)] == expected
    done = await client.get(
        f"/runs/{run_id}/events", headers={**HEADERS, "Last-Event-ID": str(full[-1]["sequence"])}
    )
    assert _events(done.text) == []  # nothing missed, and the stream still ends


async def test_the_stream_forwards_live_deltas_and_a_reconnect_replays_the_final_text(
    tmp_path: Path,
) -> None:
    words = (
        "Hello there, ",
        "this reply is long enough ",
        "to stream past the ",
        "held tail. ",
    ) * 3
    reply = "".join(words)
    pieces = [AIMessageChunk(content=p) for p in words]
    application = _application(tmp_path, FakeStreamingModel([pieces], delay=0.05))
    async for client in _client(application):
        _, run_id = await _start(client)
        live = _events((await client.get(f"/runs/{run_id}/events", headers=HEADERS)).text)

        deltas = [e for e in live if e["type"] == "assistant.delta"]
        streamed = "".join(e["data"]["text"] for e in deltas)
        assert reply.startswith(streamed) and len(reply) - len(streamed) == 64  # the held tail
        assert all(e["sse_id"] is None and e["sequence"] == -1 for e in deltas)
        logged = [e for e in live if e["type"] != "assistant.delta"]
        assert [e["type"] for e in logged] == ["run.started", "assistant.message", "run.completed"]
        assert [str(e["sequence"]) for e in logged] == [e["sse_id"] for e in logged]
        message = next(e for e in live if e["type"] == "assistant.message")
        assert live.index(deltas[-1]) < live.index(message)

        resumed = _events(
            (
                await client.get(
                    f"/runs/{run_id}/events",
                    headers={**HEADERS, "Last-Event-ID": str(logged[0]["sequence"])},
                )
            ).text
        )
        assert [e["type"] for e in resumed] == ["assistant.message", "run.completed"]
        assert resumed[0]["data"]["content"] == reply


async def test_live_deltas_redact_a_key_split_across_two_chunks(tmp_path: Path) -> None:
    key = "sk-or-v1-0123456789abcdef0123456789abcdef"
    words = ("Your key is ", key[:12], key[12:], ", keep it safe. " + "x" * 80)
    pieces = [AIMessageChunk(content=p) for p in words]
    application = _application(tmp_path, FakeStreamingModel([pieces], delay=0.05))
    async for client in _client(application):
        _, run_id = await _start(client)
        live = _events((await client.get(f"/runs/{run_id}/events", headers=HEADERS)).text)

        streamed = "".join(e["data"]["text"] for e in live if e["type"] == "assistant.delta")
        assert streamed.startswith("Your key is [redacted]")
        assert "sk-or" not in streamed and key[12:20] not in streamed


async def test_session_events_rebuild_the_whole_conversation(
    web: tuple[httpx.AsyncClient, Application],
) -> None:
    client, _ = web
    session, run_id = await _start(client)
    await client.get(f"/runs/{run_id}/events", headers=HEADERS)

    logged = (await client.get(f"/sessions/{session}/events", headers=HEADERS)).json()

    assert [e["type"] for e in logged] == ["run.started", "assistant.message", "run.completed"]


async def test_branching_forks_at_a_logged_event(
    web: tuple[httpx.AsyncClient, Application],
) -> None:
    client, _ = web
    session, run_id = await _start(client)
    events = _events((await client.get(f"/runs/{run_id}/events", headers=HEADERS)).text)
    done = next(e for e in events if e["type"] == "run.completed")

    forked = await client.post(
        f"/sessions/{session}/branch", headers=HEADERS, json={"event_id": done["id"]}
    )
    assert forked.status_code == 201 and forked.json()["id"] != session
    missing = await client.post(
        f"/sessions/{session}/branch", headers=HEADERS, json={"event_id": "nope"}
    )
    assert missing.status_code == 404


async def test_approving_an_unknown_request_is_a_conflict_not_a_crash(
    web: tuple[httpx.AsyncClient, Application],
) -> None:
    client, _ = web
    _, run_id = await _start(client)
    response = await client.post(
        f"/runs/{run_id}/approvals/nope", headers=HEADERS, json={"approve": True}
    )
    assert response.status_code == 409


# ── stop means stop: no further model calls, no further cost ─────────────────
async def test_cancel_interrupts_the_in_flight_model_call_and_ends_the_stream(
    tmp_path: Path,
) -> None:
    model = BlockingModel()
    application = _application(tmp_path, model)
    async for client in _client(application):
        _, run_id = await _start(client)
        await asyncio.wait_for(model.started.wait(), 2)
        stream = asyncio.create_task(client.get(f"/runs/{run_id}/events", headers=HEADERS))

        response = await client.post(f"/runs/{run_id}/cancel", headers=HEADERS)
        body = await asyncio.wait_for(stream, 3)  # the stream ends instead of hanging

        assert response.json() == {"runs": {run_id: "cancelled"}}
        assert model.interrupted == 1  # the request to the provider was abandoned
        await asyncio.sleep(0.3)
        assert model.calls == 1  # and nothing re-issued it
        assert body.status_code == 200


async def test_cancel_during_a_tool_loop_stops_further_model_calls(tmp_path: Path) -> None:
    sleep = SleepTool("sleep", 0.05)
    looping = FakeChatModel([ai(tool_calls=[call("sleep", id=f"c{i}")]) for i in range(10_000)])
    application = _application(tmp_path, looping, tools=[sleep])
    async for client in _client(application):
        _, run_id = await _start(client)
        await asyncio.sleep(0.3)
        assert len(looping.calls) > 1  # it is really looping

        await client.post(f"/runs/{run_id}/cancel", headers=HEADERS)
        calls_at_stop = len(looping.calls)
        await asyncio.sleep(0.4)

        assert len(looping.calls) == calls_at_stop
        status = (await client.get(f"/runs/{run_id}", headers=HEADERS)).json()["status"]
        assert status == "cancelled"


async def test_cancelling_a_parent_also_stops_its_subagent(tmp_path: Path) -> None:
    child = BlockingModel()
    parent = FakeChatModel(
        [ai(tool_calls=[call("delegate", {"agent_name": "explorer", "task": "look"})]), ai("x")]
    )
    application = _application(tmp_path, parent, child_model=child)
    async for client in _client(application):
        _, run_id = await _start(client)
        await asyncio.wait_for(child.started.wait(), 3)

        response = await client.post(f"/runs/{run_id}/cancel", headers=HEADERS)

        runs = response.json()["runs"]
        assert len(runs) == 2 and set(runs.values()) == {"cancelled"}
        assert child.interrupted == 1
        await asyncio.sleep(0.3)
        assert child.calls == 1 and len(parent.calls) == 1  # the parent never resumed either


async def test_cancel_is_idempotent_and_safe_on_a_finished_run(
    web: tuple[httpx.AsyncClient, Application],
) -> None:
    client, _ = web
    _, run_id = await _start(client)
    await client.get(f"/runs/{run_id}/events", headers=HEADERS)

    first = await client.post(f"/runs/{run_id}/cancel", headers=HEADERS)
    second = await client.post(f"/runs/{run_id}/cancel", headers=HEADERS)

    assert first.status_code == second.status_code == 200
    assert first.json() == {"runs": {run_id: "completed"}}  # a finished run is not rewritten


async def test_usage_endpoint_reports_tokens_and_dollars_and_respects_ownership(
    tmp_path: Path,
) -> None:
    from langchain_core.messages import AIMessage

    reply = AIMessage(
        content="priced",
        usage_metadata={"input_tokens": 900, "output_tokens": 40, "total_tokens": 940},
        response_metadata={"cost": 0.0031},
    )
    application = _application(tmp_path, FakeChatModel([reply]))
    async for client in _client(application):
        session, run_id = await _start(client)
        await client.get(f"/runs/{run_id}/events", headers=HEADERS)

        body = (await client.get(f"/sessions/{session}/usage", headers=HEADERS)).json()
        assert body["cost_usd"] == pytest.approx(0.0031)
        assert (body["calls"], body["input_tokens"], body["output_tokens"]) == (1, 900, 40)
        assert body["budget_usd"] is None
        assert body["summary"].startswith("$0.0031")
        assert (await client.get("/sessions/nope/usage", headers=HEADERS)).status_code == 404
        assert (await client.get(f"/sessions/{session}/usage")).status_code == 401


async def test_a_conversation_over_budget_gets_402_and_starts_no_run(tmp_path: Path) -> None:
    from dataclasses import replace

    from langchain_core.messages import AIMessage

    from agent_stack.app.config import AppConfig

    reply = AIMessage(
        content="pricey",
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        response_metadata={"cost": 0.5},
    )
    model = FakeChatModel([reply, reply])
    application = replace(_application(tmp_path, model), config=AppConfig(budget_usd=0.25))
    async for client in _client(application):
        session, run_id = await _start(client)
        await client.get(f"/runs/{run_id}/events", headers=HEADERS)

        refused = await client.post(
            f"/sessions/{session}/messages", headers=HEADERS, json={"text": "again"}
        )

        assert refused.status_code == 402 and "budget" in refused.json()["detail"]
        assert len(model.calls) == 1  # the second message never reached the model


async def test_changed_files_list_and_per_file_diff(tmp_path: Path) -> None:
    from tests.integration.test_changes import _edit, _world, _write

    model = FakeChatModel(
        [
            ai(tool_calls=[_write("docs/new.md", "# New\nbody\n", "c1")]),
            ai(tool_calls=[_edit("docs/new.md", "body", "better body", "c2")]),
            ai("done"),
        ]
    )
    world = _world(tmp_path, model)
    async for client in _client(world.app):
        session, run_id = await _start(client)
        await client.get(f"/runs/{run_id}/events", headers=HEADERS)

        listed = (await client.get(f"/sessions/{session}/changes", headers=HEADERS)).json()
        assert listed["files"] == [
            {"path": "docs/new.md", "kind": "created", "edits": 2, "added": 2, "removed": 0}
        ]
        assert "shell commands" in listed["note"]

        detail = (
            await client.get(
                f"/sessions/{session}/changes/file", params={"path": "docs/new.md"}, headers=HEADERS
            )
        ).json()
        assert detail["after"] == "# New\nbetter body\n" and detail["before"] is None
        assert "+better body" in detail["diff"] and detail["diff"].startswith("--- /dev/null")

        by_run = await client.get(
            f"/sessions/{session}/changes", params={"run_id": run_id}, headers=HEADERS
        )
        assert [f["path"] for f in by_run.json()["files"]] == ["docs/new.md"]

        # nothing changed there, a made-up path, another conversation, no token
        missing = await client.get(
            f"/sessions/{session}/changes/file",
            params={"path": "../../etc/passwd"},
            headers=HEADERS,
        )
        assert missing.status_code == 404
        assert (await client.get("/sessions/nope/changes", headers=HEADERS)).status_code == 404
        assert (await client.get(f"/sessions/{session}/changes")).status_code == 401


async def test_another_principals_changes_look_nonexistent(tmp_path: Path) -> None:
    from agent_stack.gateways.base import MessageEnvelope
    from tests.integration.test_changes import _world, _write

    world = _world(
        tmp_path, FakeChatModel([ai(tool_calls=[_write("secret.txt", "private\n")]), ai("ok")])
    )
    handle = await world.app.send_message(
        MessageEnvelope(principal="telegram:999", session="foreign", text="go")
    )
    await handle.result()
    assert [f.path for f in await world.app.changed_files("foreign")] == ["secret.txt"]
    async for client in _client(world.app):
        assert (await client.get("/sessions/foreign/changes", headers=HEADERS)).status_code == 404
        file_response = await client.get(
            "/sessions/foreign/changes/file", params={"path": "secret.txt"}, headers=HEADERS
        )
        assert file_response.status_code == 404
