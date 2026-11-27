"""P42 — OpenTelemetry spans built from the event stream (in-memory exporter)."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent_stack.core.agent import Agent, SubAgentSpec
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.run import RunManager, RunRequest
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.persistence.memory import InMemoryRunStore
from agent_stack.subagents.delegation import delegate
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry
from tests.fakes import FailingTool, FakeChatModel, SleepTool, call

sdk = pytest.importorskip("opentelemetry.sdk.trace")
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)
from opentelemetry.trace import StatusCode  # noqa: E402

from agent_stack.observability.tracing import RunTracer  # noqa: E402

SECRET = "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123456789"


def _ai(content: str = "", tool_calls: list[dict[str, Any]] | None = None) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=tool_calls or [],
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            "input_token_details": {"cache_read": 40, "cache_creation": 7},
        },
        response_metadata={"model_name": "fake/model", "cost": 0.002},
    )


class FakeFactory:
    def __init__(self, runtimes: dict[str, AgentRuntime]) -> None:
        self._runtimes = runtimes

    def create(self, agent: Agent) -> AgentRuntime:
        return self._runtimes[agent.name]


def _setup(
    parent_script: list[AIMessage], tools: list[Any], child_script: list[AIMessage] | None = None
) -> tuple[RunManager, InMemorySpanExporter, RunTracer]:
    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = RunTracer(provider.get_tracer("test"))
    bus = InMemoryEventBus(audit=tracer)
    store = InMemorySessionStore()

    def runtime(agent: Agent, model: Any, tools: list[Any]) -> AgentRuntime:
        return AgentRuntime(
            agent=agent,
            model=model,
            tool_executor=ToolExecutor(ToolRegistry(tools)),
            session_store=store,
            event_bus=bus,
        )

    explorer = Agent(name="explorer", model="test:model")
    parent = Agent(
        name="assistant",
        model="test:model",
        tools=(delegate,),
        subagents=(SubAgentSpec("explorer", "Exploration.", explorer),),
    )
    child_rt = runtime(explorer, FakeChatModel(child_script or []), [])
    manager = RunManager(
        runtime=runtime(parent, FakeChatModel(parent_script), [delegate, *tools]),
        run_store=InMemoryRunStore(),
        event_bus=bus,
        agent_factory=FakeFactory({"explorer": child_rt}),
    )
    return manager, exporter, tracer


async def test_span_tree_for_tool_and_subagent() -> None:
    manager, exporter, _ = _setup(
        [
            _ai(
                tool_calls=[
                    call("sleepy", {"api_key": SECRET, "note": f"use {SECRET}"}, id="c1"),
                    call("delegate", {"agent_name": "explorer", "task": "look"}, id="c2"),
                ]
            ),
            _ai("done"),
        ],
        [SleepTool("sleepy", 0.01)],
        child_script=[_ai("child done")],
    )
    handle = await manager.start(RunRequest(session_id="s1", user_id="u1", message="go"))
    await handle.result()

    spans = exporter.get_finished_spans()
    by_name: dict[str, list[Any]] = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(span)
    (run,) = by_name["run"]
    (sub,) = by_name["subagent"]
    tools = {s.attributes["gen_ai.tool.name"]: s for s in by_name["tool.call"]}

    assert run.parent is None
    assert {s.context.trace_id for s in spans} == {run.context.trace_id}  # one trace
    assert run.attributes["session.id"] == "s1"
    assert run.attributes["gen_ai.agent.name"] == "assistant"
    assert run.attributes["gen_ai.request.model"] == "fake/model"
    assert run.status.status_code is StatusCode.UNSET

    assert tools["sleepy"].parent.span_id == run.context.span_id
    assert tools["delegate"].parent.span_id == run.context.span_id
    assert sub.parent.span_id == tools["delegate"].context.span_id
    assert sub.attributes["gen_ai.agent.name"] == "explorer"
    assert tools["sleepy"].attributes["agent_stack.tool.ok"] is True
    assert tools["sleepy"].attributes["agent_stack.tool.duration_ms"] > 0

    models = by_name["model.call"]
    assert len(models) == 3
    parents = sorted(m.parent.span_id for m in models)
    assert parents == sorted([run.context.span_id] * 2 + [sub.context.span_id])
    first = models[0]
    assert first.attributes["gen_ai.usage.input_tokens"] == 100
    assert first.attributes["agent_stack.cache_read_tokens"] == 40
    assert first.attributes["agent_stack.cache_write_tokens"] == 7
    assert first.attributes["agent_stack.cost_usd"] == 0.002
    for span in spans:
        assert span.start_time <= span.end_time
        assert run.start_time <= span.start_time and span.end_time <= run.end_time

    # redaction: the secret never reaches an attribute
    args = json.loads(tools["sleepy"].attributes["agent_stack.tool.arguments"])
    assert args["api_key"] == "[redacted]"
    assert SECRET not in json.dumps([dict(s.attributes) for s in spans])


async def test_failed_run_and_failed_tool_end_with_error() -> None:
    manager, exporter, tracer = _setup(
        [_ai(tool_calls=[call("boom", id="c1")])],  # second model call exhausts the script
        [FailingTool()],
    )
    handle = await manager.start(RunRequest(session_id="s1", user_id="u1", message="go"))
    with pytest.raises(AssertionError):
        await handle.result()

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["run"].status.status_code is StatusCode.ERROR
    assert spans["tool.call"].status.status_code is StatusCode.ERROR
    assert spans["tool.call"].attributes["agent_stack.tool.ok"] is False
    assert not tracer._runs


async def test_hard_cancel_spans_are_ended() -> None:
    manager, exporter, tracer = _setup(
        [_ai(tool_calls=[call("sleepy", id="c1")])], [SleepTool("sleepy", 10)]
    )
    handle = await manager.start(RunRequest(session_id="s1", user_id="u1", message="go"))
    while not tracer._runs or not next(iter(tracer._runs.values())).tools:
        await asyncio.sleep(0.01)
    await manager.cancel(handle.run_id)
    # no terminal event is published for a hard cancel...
    assert "run" not in {s.name for s in exporter.get_finished_spans()}

    tracer.close()  # ...so exit (or the session's next run) ends them
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["run"].status.status_code is StatusCode.ERROR
    assert spans["tool.call"].status.status_code is StatusCode.ERROR
    assert not tracer._runs


def test_off_imports_nothing() -> None:
    """Off: no exporter is loaded, and the app does not need opentelemetry at all.

    (langsmith, a langchain dependency, imports the bare SDK by itself when it is installed,
    so the check is that ours never asks for it: with it blocked everything still works.)
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_")}
    env.pop("AGENT_STACK_OTEL", None)
    check = (
        "import os, sys\n"
        "{block}"
        "import agent_stack.app.bootstrap\n"
        "from agent_stack.observability.tracing import tracing_from_env\n"
        "assert tracing_from_env(os.environ) == ()\n"
        "assert not [m for m in sys.modules if m.startswith('opentelemetry.exporter')]\n"
        "assert 'opentelemetry.sdk.trace.export' not in sys.modules\n"
    )
    for block in ("", "sys.modules['opentelemetry'] = None\n"):
        result = subprocess.run(
            [sys.executable, "-c", check.format(block=block)],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr


def test_on_builds_a_sink() -> None:
    from agent_stack.observability.tracing import tracing_from_env

    (sink,) = tracing_from_env({"AGENT_STACK_OTEL": "1", "OTEL_TRACES_EXPORTER": "console"})
    assert isinstance(sink, RunTracer)
