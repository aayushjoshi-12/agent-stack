"""P51 model retries and failover: transient errors are asked again with backoff, others are not,
cancellation stops a backoff, and a fallback model answers (and is billed) when the first gives up."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, AIMessageChunk

import agent_stack.core.runtime as runtime_module
from agent_stack.core.agent import Agent
from agent_stack.core.cancellation import CancellationToken
from agent_stack.core.errors import RunCancelled
from agent_stack.core.event_bus import InMemoryEventBus
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.runtime import AgentRuntime
from agent_stack.core.session import InMemorySessionStore
from agent_stack.models.retry import retry_after, transient
from agent_stack.tools.executor import ToolExecutor
from agent_stack.tools.registry import ToolRegistry


class ProviderError(Exception):
    """Shaped like the OpenRouter SDK's errors: a status code and the response headers."""

    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.headers = headers or {}


class Bus(InMemoryEventBus):
    def __init__(self) -> None:
        super().__init__()
        self.published: list[AgentEvent] = []

    async def publish(self, event: AgentEvent) -> None:
        self.published.append(event)
        await super().publish(event)


class Flaky:
    """Each call takes the next scripted item: an exception is raised, a message is returned."""

    def __init__(self, script: Sequence[Exception | AIMessage]) -> None:
        self.script = list(script)
        self.calls = 0

    def bind_tools(self, tools: Any) -> Flaky:
        return self

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class StreamsThenDrops(Flaky):
    """Streams one chunk, then the connection drops."""

    async def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[AIMessageChunk]:
        self.calls += 1
        yield AIMessageChunk(content="half an answer")
        raise httpx.ReadError("connection reset")


def _runtime(model: Any, fallback: Any = None, bus: Bus | None = None) -> AgentRuntime:
    return AgentRuntime(
        agent=Agent(
            name="assistant",
            model="test:primary",
            fallback_model="test:fallback" if fallback is not None else None,
            system_prompt="t",
        ),
        model=model,
        fallback_model=fallback,
        tool_executor=ToolExecutor(ToolRegistry([])),
        session_store=InMemorySessionStore(),
        event_bus=bus,
    )


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module, "_BACKOFF_CAP", 0.01)


async def _run(runtime: AgentRuntime, cancel: CancellationToken | None = None) -> AIMessage:
    return await runtime.run(session_id="s", user_id="u", message="hi", cancel=cancel)


def _retries(bus: Bus) -> list[dict[str, Any]]:
    return [dict(e.data) for e in bus.published if e.type is EventType.MODEL_RETRY]


def test_transient_is_rate_limits_server_errors_and_dropped_connections() -> None:
    assert transient(ProviderError(429)) and transient(ProviderError(503))
    assert not transient(ProviderError(400)) and not transient(ProviderError(401))
    try:
        try:
            raise httpx.ConnectError("refused")
        except httpx.ConnectError as inner:
            raise RuntimeError("provider wrapper") from inner
    except RuntimeError as wrapped:
        assert transient(wrapped)
    assert not transient(ValueError("context too long"))
    assert retry_after(ProviderError(429, {"retry-after": "2"})) == 2.0
    assert retry_after(ProviderError(429, {"retry-after-ms": "250"})) == 0.25
    assert retry_after(ProviderError(429, {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"})) is None


async def test_a_transient_error_is_retried_until_the_model_answers() -> None:
    bus = Bus()
    model = Flaky([ProviderError(503), httpx.ReadTimeout("slow"), AIMessage(content="ok")])
    reply = await _run(_runtime(model, bus=bus))
    assert reply.content == "ok"
    assert model.calls == 3
    assert [r["attempt"] for r in _retries(bus)] == [1, 2]
    assert not any(r["fallback"] for r in _retries(bus))


async def test_retry_after_is_honoured() -> None:
    bus = Bus()
    model = Flaky([ProviderError(429, {"retry-after": "0.3"}), AIMessage(content="ok")])
    started = time.monotonic()
    await _run(_runtime(model, bus=bus))
    assert time.monotonic() - started >= 0.3
    assert _retries(bus)[0]["delay"] == 0.3


async def test_a_bad_request_is_not_retried() -> None:
    model = Flaky([ProviderError(400), AIMessage(content="never")])
    with pytest.raises(ProviderError):
        await _run(_runtime(model))
    assert model.calls == 1


async def test_retries_give_up_after_the_limit() -> None:
    model = Flaky([ProviderError(502)] * (runtime_module._RETRIES + 1))
    with pytest.raises(ProviderError):
        await _run(_runtime(model))
    assert model.calls == runtime_module._RETRIES + 1


async def test_cancelling_during_a_backoff_stops_the_run() -> None:
    cancel = CancellationToken()
    model = Flaky([ProviderError(429, {"retry-after": "30"}), AIMessage(content="late")])
    task = asyncio.create_task(_run(_runtime(model), cancel))
    await asyncio.sleep(0.1)
    cancel.cancel()
    started = time.monotonic()
    with pytest.raises(RunCancelled):
        await task
    assert time.monotonic() - started < 1


async def test_the_fallback_answers_and_its_usage_is_recorded() -> None:
    bus = Bus()
    primary = Flaky([ProviderError(404)])  # a bad model id: straight to the fallback
    answer = AIMessage(
        content="from the fallback",
        usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        response_metadata={"model_name": "fallback-model", "cost": 0.001},
    )
    fallback = Flaky([ProviderError(503), answer])
    reply = await _run(_runtime(primary, fallback, bus=bus))
    assert reply.content == "from the fallback"
    assert primary.calls == 1 and fallback.calls == 2
    retries = _retries(bus)
    assert retries[0]["fallback"] and retries[0]["model"] == "test:fallback"
    assert retries[1] == retries[1] | {"fallback": False, "model": "test:fallback", "attempt": 1}
    usage = [e.data for e in bus.published if e.type is EventType.MODEL_USAGE]
    assert usage[0]["model"] == "fallback-model" and usage[0]["cost_usd"] == 0.001


async def test_a_stream_that_already_showed_text_is_not_retried() -> None:
    model = StreamsThenDrops([])
    with pytest.raises(httpx.ReadError):
        await _run(_runtime(model, Flaky([AIMessage(content="never")]), bus=Bus()))
    assert model.calls == 1


def test_openrouter_models_have_the_sdk_retries_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_stack.models.factory import build_model

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    model: Any = build_model("openrouter:z-ai/glm-5.3-flash", client_retries=False)
    assert model.max_retries == 0
    assert model.client.sdk_configuration.retry_config.strategy == "none"
    side: Any = build_model("openrouter:z-ai/glm-5.3-flash")  # titles, auto mode: client retries
    assert side.max_retries == 2
