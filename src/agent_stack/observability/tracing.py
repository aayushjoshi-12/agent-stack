"""OpenTelemetry traces built from the event stream (P42).

``RunTracer`` is a bus sink like ``Metrics``: it turns the events the runtime already emits
into spans, so the runtime carries no tracing code. One trace per top-level run:

- ``run`` (root): session id, run id, agent, model (from the first model call).
- ``model.call``: tokens, cache reads/writes, cost, model. ``MODEL_USAGE`` arrives after the call, so
  the span starts at the previous event of the same run (the run start, the last tool result,
  ...) and ends at the usage event. A usage with a ``purpose`` (the conversation title, made
  beside the run) has no known start and is a zero-length span.
- ``tool.call``: tool name, call id, arguments (sanitised exactly as the audit log does), ok,
  duration.
- ``subagent``: a child run, under the parent's ``delegate`` tool span. ``SUBAGENT_STARTED``
  (published before the child's first event) binds the child to the parent's oldest open
  ``delegate`` span not yet bound; with several delegate calls in flight the pairing is in
  start order.

Every span gets explicit start/end times from event timestamps. A hard-cancelled run
publishes no terminal event (by design), so its spans are ended by whatever comes next: a
child's ``SUBAGENT_COMPLETED`` on the parent, the next run in the same session (a session
runs one at a time), the parent run ending, or ``close()`` at exit. Such spans end at the
run's last seen event with an ERROR status.

Off unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` (or ``..._TRACES_ENDPOINT``) or ``AGENT_STACK_OTEL=1``
is set; when off nothing from ``opentelemetry`` is imported.
"""

from __future__ import annotations

import atexit
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from agent_stack.core.events import AgentEvent, EventType
from agent_stack.observability.audit import audit_record
from agent_stack.observability.redact import env_secrets, redact

if TYPE_CHECKING:
    from opentelemetry.trace import Span, Tracer

__all__ = ["RunTracer", "tracing_from_env"]

_LOG = logging.getLogger(__name__)
_DELEGATE = "delegate"


def _ns(ts: datetime) -> int:
    return int(ts.timestamp()) * 1_000_000_000 + ts.microsecond * 1_000


@dataclass
class _Run:
    span: Span
    session_id: str
    parent_run_id: str | None
    last_ns: int
    model_set: bool = False
    tools: dict[str, tuple[Span, int]] = field(default_factory=dict)  # call id -> (span, start)
    delegates: list[str] = field(default_factory=list)  # open delegate call ids, unbound


class RunTracer:
    """Event-bus sink that turns run events into OpenTelemetry spans."""

    def __init__(self, tracer: Tracer) -> None:
        from opentelemetry import context, trace
        from opentelemetry.trace import Status, StatusCode

        self._tracer = tracer
        self._trace = trace
        self._root = context.Context()  # empty: every top-level run starts a new trace
        self._status, self._code = Status, StatusCode
        self._known = env_secrets()
        self._runs: dict[str, _Run] = {}
        self._child_parent: dict[str, Span] = {}  # child run id -> its delegate tool span

    def write(self, event: AgentEvent) -> None:
        kind, data, now = event.type, event.data, _ns(event.timestamp)
        if kind is EventType.RUN_STARTED:
            self._start_run(event, now)
            return
        run = self._runs.get(event.run_id)
        if run is None:
            return  # a run that started before tracing, or a late event after its end
        if kind is EventType.MODEL_USAGE:
            self._model_call(run, event, now)
        elif kind is EventType.TOOL_STARTED:
            self._tool_started(run, event, now)
        elif kind is EventType.TOOL_COMPLETED:
            self._tool_completed(run, event, now)
        elif kind is EventType.SUBAGENT_STARTED:
            if run.delegates:
                call_id = run.delegates.pop(0)
                self._child_parent[str(data.get("child_run_id"))] = run.tools[call_id][0]
        elif kind is EventType.SUBAGENT_COMPLETED:
            child = str(data.get("child_run_id"))
            if child in self._runs:  # it published no terminal event: a hard cancel
                self._end_run(child, error=f"subagent {data.get('status', 'cancelled')}")
        elif kind is EventType.RUN_COMPLETED:
            run.last_ns = now
            self._end_run(event.run_id, error=None)
            return
        elif kind is EventType.RUN_FAILED:
            run.last_ns = now
            self._end_run(event.run_id, error=self._clean(str(data.get("error", "failed"))))
            return
        if not (kind is EventType.MODEL_USAGE and data.get("purpose")):
            run.last_ns = now

    def close(self) -> None:
        """End every span still open (runs that never finished before exit)."""
        for run_id in list(self._runs):
            self._end_run(run_id, error="not finished")

    def _start_run(self, event: AgentEvent, now: int) -> None:
        parent = event.parent_run_id
        if parent is None:
            # sessions run one at a time: an open run here was hard-cancelled
            for run_id, run in list(self._runs.items()):
                if run.parent_run_id is None and run.session_id == event.session_id:
                    self._end_run(run_id, error="cancelled")
            name, ctx = "run", self._root
        else:
            name = "subagent"
            owner = self._child_parent.pop(event.run_id, None)
            if owner is None and parent in self._runs:
                owner = self._runs[parent].span
            ctx = self._trace.set_span_in_context(owner) if owner is not None else self._root
        attributes: dict[str, Any] = {
            "session.id": event.session_id,
            "agent_stack.run_id": event.run_id,
            "gen_ai.agent.name": str(event.data.get("agent", "")),
        }
        if parent is not None:
            attributes["agent_stack.parent_run_id"] = parent
        span = self._tracer.start_span(name, context=ctx, attributes=attributes, start_time=now)
        self._runs[event.run_id] = _Run(span, event.session_id, parent, now)

    def _model_call(self, run: _Run, event: AgentEvent, now: int) -> None:
        data = event.data
        model = data.get("model")
        if model and not data.get("purpose") and not run.model_set:
            run.span.set_attribute("gen_ai.request.model", str(model))
            run.model_set = True
        attributes: dict[str, Any] = {
            "gen_ai.usage.input_tokens": int(data.get("input_tokens", 0)),
            "gen_ai.usage.output_tokens": int(data.get("output_tokens", 0)),
            "agent_stack.cache_read_tokens": int(data.get("cache_read_tokens", 0)),
            "agent_stack.cache_write_tokens": int(data.get("cache_write_tokens", 0)),
            "agent_stack.reasoning_tokens": int(data.get("reasoning_tokens", 0)),
        }
        if model:
            attributes["gen_ai.response.model"] = str(model)
        if data.get("cost_usd") is not None:
            attributes["agent_stack.cost_usd"] = float(data["cost_usd"])
        if data.get("purpose"):
            attributes["agent_stack.purpose"] = str(data["purpose"])
        start = now if data.get("purpose") else run.last_ns
        span = self._tracer.start_span(
            "model.call",
            context=self._trace.set_span_in_context(run.span),
            attributes=attributes,
            start_time=start,
        )
        span.end(end_time=now)

    def _tool_started(self, run: _Run, event: AgentEvent, now: int) -> None:
        call_id, tool = str(event.data.get("tool_call_id")), str(event.data.get("tool"))
        args = audit_record(event, self._known)["data"].get("args", {})
        span = self._tracer.start_span(
            "tool.call",
            context=self._trace.set_span_in_context(run.span),
            attributes={
                "gen_ai.tool.name": tool,
                "gen_ai.tool.call.id": call_id,
                "agent_stack.tool.arguments": json.dumps(args, ensure_ascii=False, default=str),
            },
            start_time=now,
        )
        run.tools[call_id] = (span, now)
        if tool == _DELEGATE:
            run.delegates.append(call_id)

    def _tool_completed(self, run: _Run, event: AgentEvent, now: int) -> None:
        call_id = str(event.data.get("tool_call_id"))
        opened = run.tools.pop(call_id, None)
        if call_id in run.delegates:
            run.delegates.remove(call_id)
        if opened is None:
            return
        span, start = opened
        ok = event.data.get("ok") is not False
        span.set_attribute("agent_stack.tool.ok", ok)
        span.set_attribute("agent_stack.tool.duration_ms", (now - start) / 1_000_000)
        if not ok:
            error = self._clean(str(event.data.get("error") or "tool failed"))
            span.set_status(self._status(self._code.ERROR, error))
        span.end(end_time=now)

    def _end_run(self, run_id: str, *, error: str | None) -> None:
        run = self._runs.pop(run_id, None)
        if run is None:
            return
        for child_id, child in list(self._runs.items()):
            if child.parent_run_id == run_id:
                self._end_run(child_id, error=error or "parent ended")
        for span, _ in run.tools.values():
            span.set_status(self._status(self._code.ERROR, error or "unfinished"))
            span.end(end_time=run.last_ns)
        if error is not None:
            run.span.set_status(self._status(self._code.ERROR, error))
        run.span.end(end_time=run.last_ns)

    def _clean(self, text: str) -> str:
        return redact(text, self._known)[:2000]


def tracing_from_env(env: Mapping[str, str]) -> tuple[RunTracer, ...]:
    """The tracing sink when tracing is switched on (and the ``otel`` extra installed), else ``()``.

    ``env`` is the process environment (the SDK reads the rest of it). Standard ``OTEL_*``
    variables apply (endpoint, headers, ``OTEL_SERVICE_NAME``,
    ``OTEL_RESOURCE_ATTRIBUTES``); ``OTEL_TRACES_EXPORTER=console`` prints spans instead.
    """
    if not (
        env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
        or env.get("AGENT_STACK_OTEL") == "1"
    ):
        return ()
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except ImportError:
        _LOG.warning("tracing is on but the otel extra is missing: uv sync --extra otel")
        return ()
    named = "OTEL_SERVICE_NAME" in env or "service.name=" in env.get("OTEL_RESOURCE_ATTRIBUTES", "")
    resource = Resource.create({} if named else {"service.name": "agent-stack"})
    provider = TracerProvider(resource=resource)  # registers its own flush at exit
    console = env.get("OTEL_TRACES_EXPORTER") == "console"
    exporter = ConsoleSpanExporter() if console else OTLPSpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    tracer = RunTracer(provider.get_tracer("agent_stack"))
    atexit.register(tracer.close)  # registered after the provider's: runs before its flush
    return (tracer,)
