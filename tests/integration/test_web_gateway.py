"""P24 web gateway: auth, ownership, resumable SSE, and a stop that really stops spending."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

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
from tests.fakes import FakeChatModel, SleepTool, ai, call

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
