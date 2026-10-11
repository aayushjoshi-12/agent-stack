"""Unit tests for P8b: the audit log written by the event bus.

The bus is the single choke point, so a full run through the real run manager proves
every published event gets exactly one line. Cap/redaction and restart-replay are
tested against ``AuditLogger`` directly.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent_stack.core.agent import Agent
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.limits import AgentLimits
from agent_stack.core.run import RunManager, RunRequest
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.observability.audit import AuditLogger, audit_log_path, read_audit
from agent_stack.observability.cli import _matches
from agent_stack.persistence.memory import InMemoryRunStore
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FakeChatModel, ai, call


class EchoTool:
    """A tiny duck-typed tool; the registry/executor only need name/description/ainvoke."""

    name = "echo"
    description = "Echo the arguments back."

    async def ainvoke(self, args: dict[str, Any]) -> str:
        return f"echo: {args}"


def _build_runtime(
    model: Any,
    tools: Sequence[Any],
    store: InMemorySessionStore,
    bus: InMemoryEventBus,
) -> AgentRuntime:
    registry = ToolRegistry(tools)  # type: ignore[arg-type]
    agent = Agent(
        name="assistant",
        model="test:model",
        tools=tuple(tools),  # type: ignore[arg-type]
        system_prompt="You are a test assistant.",
        limits=AgentLimits(),
    )
    return AgentRuntime(
        agent=agent,
        model=model,
        tool_executor=ToolExecutor(registry),
        session_store=store,
        event_bus=bus,
    )


async def test_every_published_event_has_one_audit_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    audit = AuditLogger(path)
    bus = InMemoryEventBus(audit=audit)
    store = InMemorySessionStore()
    model = FakeChatModel([ai(tool_calls=[call("echo", {"x": 1})]), ai("done")])
    runtime = _build_runtime(model, [EchoTool()], store, bus)
    manager = RunManager(runtime=runtime, run_store=InMemoryRunStore(), event_bus=bus)

    handle = await manager.start(RunRequest(session_id="s1", user_id="u1", message="go"))
    await handle.result()
    events = [event async for event in handle.events()]
    audit.close()

    lines = read_audit(path)
    assert len(lines) == len(events)
    assert [line["sequence"] for line in lines] == [event.sequence for event in events]
    assert [line["type"] for line in lines] == [event.type.value for event in events]
    assert [line["run_id"] for line in lines] == [handle.run_id] * len(lines)
    assert [line["session_id"] for line in lines] == ["s1"] * len(lines)


def test_audit_file_survives_restart_and_replays_in_order(tmp_path: Path) -> None:
    path = audit_log_path(tmp_path)
    first = AuditLogger(path)
    for sequence, event_type in enumerate((EventType.RUN_STARTED, EventType.RUN_COMPLETED)):
        first.write(AgentEvent.create(event_type, run_id="r1", session_id="s1", sequence=sequence))
    first.close()

    # A new process reopens the same file; the old lines are intact and still ordered.
    second = AuditLogger(path)
    second.write(AgentEvent.create(EventType.RUN_STARTED, run_id="r2", session_id="s2", sequence=0))
    second.close()

    run_one = [line for line in read_audit(path) if line["run_id"] == "r1"]
    assert [line["sequence"] for line in run_one] == [0, 1]
    assert [line["type"] for line in run_one] == ["run.started", "run.completed"]


def test_data_is_capped_and_redacted(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    logger = AuditLogger(path)
    logger.write(
        AgentEvent.create(
            EventType.TOOL_COMPLETED,
            run_id="r1",
            session_id="s1",
            sequence=0,
            data={
                "api_key": "sk-live-secret",
                "nested": {"password": "hunter2", "keep": "ok"},
                "output": "x" * 5000,
            },
        )
    )
    logger.close()

    text = path.read_text()
    assert "sk-live-secret" not in text
    assert "hunter2" not in text

    record = json.loads(text.splitlines()[0])
    assert record["data"]["api_key"] == "[redacted]"
    assert record["data"]["nested"]["password"] == "[redacted]"
    assert record["data"]["nested"]["keep"] == "ok"
    assert "[truncated" in record["data"]["output"]
    assert len(record["data"]["output"].encode()) < 5000


def test_cli_filter_matches_run_and_sequence() -> None:
    assert _matches({"run_id": "r1", "sequence": 3}, "r1", 3)
    assert not _matches({"run_id": "r1", "sequence": 2}, "r1", 3)
    assert not _matches({"run_id": "r2", "sequence": 9}, "r1", 0)
    assert _matches({"run_id": "r2", "sequence": 0}, None, 0)
