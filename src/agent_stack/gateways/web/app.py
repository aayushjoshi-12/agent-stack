"""The web gateway: HTTP commands plus a resumable Server-Sent Events stream.

Like every gateway it is an adapter over ``Application``; it never touches tools, models or
stores. Security: the API can run tools on this machine, so

* it binds to loopback by default,
* every route except ``/health`` needs ``Authorization: Bearer <AGENT_STACK_WEB_TOKEN>`` and the
  server refuses to start without a token (fail closed),
* a session or run that belongs to another principal is reported as not found,
* CORS allows only the configured origins (the Next.js dev server by default).

The event stream reads the durable event log (P29), so a client that drops and reconnects with
``Last-Event-ID`` (or ``?last_event_sequence=``) receives exactly the events it missed.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from agent_stack.app.application import Application, parse_ref
from agent_stack.core.errors import (
    ApprovalError,
    BudgetExceededError,
    ConfigurationError,
    PersistenceError,
)
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.core.run import Run
from agent_stack.gateways.base import HistoryItem, MessageEnvelope, model_breakdown, usage_summary

__all__ = ["WebConfigError", "WebSettings", "create_app", "run_web"]

_MIN_TOKEN = 16
_TERMINAL_EVENTS = frozenset({EventType.RUN_COMPLETED, EventType.RUN_FAILED})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_POLL_SECONDS = 0.1
_KEEPALIVE_SECONDS = 15.0
_DEFAULT_ORIGINS = ("http://localhost:3000", "http://127.0.0.1:3000")


class WebConfigError(ValueError):
    """The web gateway is not configured safely (no usable token)."""


@dataclass(frozen=True)
class WebSettings:
    """Where the API listens, who may call it and which identity its sessions act as.

    ``principal`` defaults to the TUI's identity so the web UI shows the same conversations.
    """

    token: str = field(repr=False)
    principal: str = "tui:local-user"
    host: str = "127.0.0.1"
    port: int = 8765
    origins: tuple[str, ...] = _DEFAULT_ORIGINS

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> WebSettings:
        token = env.get("AGENT_STACK_WEB_TOKEN", "").strip()
        if len(token) < _MIN_TOKEN:
            raise WebConfigError(
                f"AGENT_STACK_WEB_TOKEN must be set to a secret of at least {_MIN_TOKEN} "
                "characters: the API runs tools on this machine"
            )
        try:
            port = int(env.get("AGENT_STACK_WEB_PORT", "8765"))
        except ValueError:
            raise WebConfigError("AGENT_STACK_WEB_PORT must be a number") from None
        origins = tuple(
            o.strip() for o in env.get("AGENT_STACK_WEB_ORIGINS", "").split(",") if o.strip()
        )
        return cls(
            token=token,
            principal=env.get("AGENT_STACK_WEB_PRINCIPAL") or "tui:local-user",
            host=env.get("AGENT_STACK_WEB_HOST") or "127.0.0.1",
            port=port,
            origins=origins or _DEFAULT_ORIGINS,
        )


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)


class TitleBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class BranchBody(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)


class ApprovalBody(BaseModel):
    approve: bool


class RewindBody(BaseModel):
    """Where to go back to: the turn's owner conversation and its start event (from /turns)."""

    owner_session_id: str = Field(min_length=1, max_length=200)
    started_event_id: str = Field(min_length=1, max_length=100)
    conversation: bool = True
    code: bool = True
    force: bool = False


class ReferenceBody(BaseModel):
    reference: str = Field(min_length=1, max_length=1000)


class ModeBody(BaseModel):
    mode: str = Field(min_length=1, max_length=20)


class ModelBody(BaseModel):
    model: str = Field(min_length=1, max_length=200)


class CompactBody(BaseModel):
    focus: str = Field(default="", max_length=500)


class PointBody(BaseModel):
    point: str | None = Field(default=None, max_length=100)  # a turn number or event id


def _moves_json(files: Any) -> dict[str, Any]:
    return {
        "moved": [{"path": m.path, "action": m.action} for m in files.moved],
        "conflicts": list(files.conflicts),
    }


def _event_json(event: AgentEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": str(event.type),
        "run_id": event.run_id,
        "session_id": event.session_id,
        "sequence": event.sequence,
        "timestamp": event.timestamp.isoformat(),
        "parent_run_id": event.parent_run_id,
        "data": dict(event.data),
    }


def _history_json(item: HistoryItem) -> dict[str, Any]:
    return {
        "kind": item.kind,
        "text": item.text,
        "args": None if item.args is None else dict(item.args),
        "result": item.result,
    }


def _sse(event: AgentEvent) -> str:
    return f"id: {event.sequence}\nevent: {event.type}\ndata: {json.dumps(_event_json(event))}\n\n"


def create_app(application: Application, settings: WebSettings) -> FastAPI:
    """Build the FastAPI app over a wired ``Application``."""
    api = FastAPI(title="agent-stack", docs_url=None, redoc_url=None, openapi_url=None)
    api.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.origins),
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    )
    principal = settings.principal

    async def authorized(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, supplied = (authorization or "").partition(" ")
        ok = scheme.lower() == "bearer" and secrets.compare_digest(
            supplied.encode(), settings.token.encode()
        )
        if not ok:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing or wrong bearer token")

    auth = [Depends(authorized)]

    async def owned_session(session_id: str) -> None:
        session = await application.get_session(session_id)
        if session is None or session.owner_id != principal:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such session")

    async def owned_run(run_id: str) -> Run:
        run = await application.get_run(run_id)
        if run is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such run")
        await owned_session(run.session_id)
        return run

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ── sessions ─────────────────────────────────────────────────────────────
    @api.get("/sessions", dependencies=auth)
    async def list_sessions(
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
    ) -> list[dict[str, str | None]]:
        sessions = await application.list_sessions(principal, limit)
        return [
            {"id": s.id, "title": s.title, "updated_at": s.updated_at, "branch_of": s.branch_of}
            for s in sessions
        ]

    @api.post("/sessions", dependencies=auth, status_code=status.HTTP_201_CREATED)
    async def new_session() -> dict[str, str]:
        return {"id": await application.new_session(principal)}

    @api.get("/sessions/{session_id}", dependencies=auth)
    async def get_session(session_id: str) -> dict[str, Any]:
        await owned_session(session_id)
        session = await application.get_session(session_id)
        history = await application.session_history(session_id)
        return {
            "id": session_id,
            "title": session.title if session else None,
            "branch_of": session.branch_of if session else None,
            "history": [_history_json(h) for h in history],
        }

    @api.patch("/sessions/{session_id}", dependencies=auth)
    async def rename(session_id: str, body: TitleBody) -> dict[str, str]:
        await owned_session(session_id)
        await application.rename_session(session_id, body.title)
        return {"id": session_id, "title": body.title}

    @api.delete("/sessions/{session_id}", dependencies=auth, status_code=status.HTTP_204_NO_CONTENT)
    async def delete(session_id: str) -> None:
        await owned_session(session_id)
        await application.delete_session(session_id)

    @api.get("/sessions/{session_id}/events", dependencies=auth)
    async def session_events(session_id: str) -> list[dict[str, Any]]:
        """Every logged event of the session (all runs), for rebuilding the UI on reload."""
        await owned_session(session_id)
        return [_event_json(e) for e in await application.replay(session_id=session_id)]

    @api.get("/sessions/{session_id}/usage", dependencies=auth)
    async def usage(session_id: str) -> dict[str, Any]:
        """Tokens and dollars this conversation has consumed, subagents included."""
        await owned_session(session_id)
        u = await application.session_usage(session_id)
        return {
            "calls": u.calls,
            "priced_calls": u.priced_calls,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "reasoning_tokens": u.reasoning_tokens,
            "cost_usd": u.cost_usd,
            "budget_usd": application.config.budget_usd,
            "by_model": dict(u.by_model),
            "models_summary": model_breakdown(u),
            "summary": usage_summary(u),
        }

    @api.get("/sessions/{session_id}/changes", dependencies=auth)
    async def changes(
        session_id: str, run_id: Annotated[str | None, Query(max_length=100)] = None
    ) -> dict[str, Any]:
        """Files the agent created or edited (net of every edit), for a conversation or one run.

        Only ``write_file``/``edit_file`` are tracked: a file a shell command changed is not
        listed, which the response says so the UI can too.
        """
        await owned_session(session_id)
        if run_id is not None:
            await owned_run(run_id)
        files = await application.changed_files(session_id, run_id)
        return {
            "files": [
                {
                    "path": f.path,
                    "kind": f.kind,
                    "edits": f.edits,
                    "added": f.added,
                    "removed": f.removed,
                }
                for f in files
            ],
            "note": "Only files written or edited by the agent's file tools are listed, "
            "not files changed by shell commands.",
        }

    @api.get("/sessions/{session_id}/changes/file", dependencies=auth)
    async def changed_file(
        session_id: str,
        path: Annotated[str, Query(min_length=1, max_length=1000)],
        run_id: Annotated[str | None, Query(max_length=100)] = None,
    ) -> dict[str, Any]:
        """One changed file: its unified diff and the text before and after."""
        await owned_session(session_id)
        if run_id is not None:
            await owned_run(run_id)
        diff = await application.changed_file(session_id, path, run_id)
        if diff is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "the agent did not change that file")
        return {
            "path": diff.path,
            "kind": diff.kind,
            "edits": diff.edits,
            "added": diff.added,
            "removed": diff.removed,
            "diff": diff.diff,
            "diff_truncated": diff.diff_truncated,
            "before": diff.before,
            "after": diff.after,
        }

    @api.post("/sessions/{session_id}/branch", dependencies=auth, status_code=201)
    async def branch(session_id: str, body: BranchBody) -> dict[str, str]:
        await owned_session(session_id)
        try:
            return {"id": await application.branch_session(session_id, body.event_id)}
        except PersistenceError:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "no such event in this session"
            ) from None

    # ── slash commands and approval mode ─────────────────────────────────────
    @api.get("/commands", dependencies=auth)
    async def commands() -> list[dict[str, str]]:
        """The skills a user can invoke by typing ``/name`` (the page adds its own built-ins)."""
        return [
            {"name": c.name, "description": c.description, "kind": c.kind}
            for c in application.slash_commands()
        ]

    @api.get("/approval-mode", dependencies=auth)
    async def get_mode() -> dict[str, Any]:
        return {"mode": application.get_approval_mode(), "modes": list(application.approval_modes)}

    @api.put("/approval-mode", dependencies=auth)
    async def set_mode(body: ModeBody) -> dict[str, str]:
        try:
            return {"mode": application.set_approval_mode(body.mode)}
        except (ApprovalError, RuntimeError) as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None

    @api.get("/model", dependencies=auth)
    async def get_model() -> dict[str, str]:
        return {"model": application.get_model()}

    @api.put("/model", dependencies=auth)
    async def set_model(body: ModelBody) -> dict[str, str]:
        try:
            return {"model": application.set_model(body.model)}
        except ConfigurationError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)[:300]) from None

    @api.post("/sessions/{session_id}/compact", dependencies=auth)
    async def compact(session_id: str, body: CompactBody) -> dict[str, int]:
        """Summarise the conversation so far in place (waits for a running turn to finish)."""
        await owned_session(session_id)
        return {"messages": await application.compact_session(session_id, body.focus)}

    # ── history: rewind, branches, references ────────────────────────────────
    @api.get("/sessions/{session_id}/turns", dependencies=auth)
    async def turns(session_id: str) -> list[dict[str, Any]]:
        """Every turn that led to this conversation, across its forks, oldest first."""
        await owned_session(session_id)
        try:
            lineage = await application.timeline.lineage(session_id)
        except (PersistenceError, RuntimeError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        return [
            {
                "owner_session_id": item.session_id,
                "started_event_id": item.turn.started_event_id,
                "completed_event_id": item.turn.completed_event_id,
                "timestamp": item.turn.timestamp.isoformat(),
                "user_text": item.turn.user_text,
                "files_changed": item.turn.files_changed,
            }
            for item in lineage
        ]

    @api.post("/sessions/{session_id}/rewind", dependencies=auth)
    async def rewind(session_id: str, body: RewindBody) -> dict[str, Any]:
        await owned_session(session_id)
        lineage = await application.timeline.lineage(session_id)
        target = next(
            (
                t
                for t in lineage
                if t.session_id == body.owner_session_id
                and t.turn.started_event_id == body.started_event_id
            ),
            None,
        )
        if target is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such turn in this conversation")
        try:
            outcome = await application.timeline.rewind_to(
                session_id,
                target,
                conversation=body.conversation,
                code=body.code,
                force=body.force,
            )
        except (PersistenceError, RuntimeError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None
        return {
            "session_id": outcome.session_id,
            "prompt": outcome.prompt,
            "files": _moves_json(outcome.files),
        }

    @api.get("/sessions/{session_id}/tree", dependencies=auth)
    async def tree(session_id: str) -> dict[str, str]:
        await owned_session(session_id)
        try:
            return {
                "text": await application.timeline.tree_text(session_id),
                "mermaid": await application.timeline.tree_mermaid(session_id),
            }
        except (PersistenceError, RuntimeError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    @api.get("/sessions/{session_id}/files/status", dependencies=auth)
    async def files_status(session_id: str) -> dict[str, bool]:
        await owned_session(session_id)
        return {"differs": await application.timeline.files_differ(session_id)}

    @api.post("/sessions/{session_id}/files/restore", dependencies=auth)
    async def files_restore(session_id: str) -> dict[str, Any]:
        await owned_session(session_id)
        try:
            return _moves_json(await application.timeline.restore_files(session_id))
        except (PersistenceError, RuntimeError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from None

    @api.post("/sessions/{session_id}/reference", dependencies=auth)
    async def make_reference(session_id: str, body: PointBody) -> dict[str, str]:
        await owned_session(session_id)
        timeline = application.timeline
        try:
            point = body.point
            if point is None:
                done = [t for t in await timeline.turns(session_id) if t.completed_event_id]
                if not done:
                    raise PersistenceError("nothing has finished in this conversation yet")
                point = str(done[-1].index)
            return {"reference": await timeline.reference(session_id, point)}
        except PersistenceError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    async def referenced_session(reference: str) -> str:
        try:
            session_id, _ = parse_ref(reference)
        except PersistenceError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from None
        await owned_session(session_id)  # a reference only opens conversations you own
        return session_id

    @api.post("/references/describe", dependencies=auth)
    async def describe_reference(body: ReferenceBody) -> dict[str, str]:
        await referenced_session(body.reference)
        try:
            return {"text": await application.timeline.describe(body.reference)}
        except PersistenceError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    @api.post("/references/open", dependencies=auth, status_code=201)
    async def open_reference(body: ReferenceBody) -> dict[str, str]:
        await referenced_session(body.reference)
        try:
            return {"id": await application.timeline.open_reference(body.reference)}
        except PersistenceError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from None

    @api.post("/sessions/{session_id}/messages", dependencies=auth, status_code=202)
    async def send(session_id: str, body: MessageBody) -> dict[str, str]:
        await owned_session(session_id)
        await application.set_session_title(
            session_id, body.text.strip()[:80]
        )  # first message only
        try:
            handle = await application.send_message(
                MessageEnvelope(principal=principal, session=session_id, text=body.text)
            )
        except BudgetExceededError as exc:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from None
        return {"run_id": handle.run_id}

    # ── runs ─────────────────────────────────────────────────────────────────
    @api.get("/runs/{run_id}", dependencies=auth)
    async def get_run(run_id: str) -> dict[str, Any]:
        run = await owned_run(run_id)
        return {
            "id": run.id,
            "session_id": run.session_id,
            "status": str(run.status),
            "steps": run.step_count,
            "tool_calls": run.tool_call_count,
            "error": run.error,
            "parent_run_id": run.parent_run_id,
        }

    @api.post("/runs/{run_id}/input", dependencies=auth)
    async def send_input(run_id: str, body: MessageBody) -> dict[str, bool]:
        """Queue a message into a working run; ``accepted`` is false once it has finished."""
        await owned_run(run_id)
        return {"accepted": await application.send_input(run_id, body.text)}

    @api.post("/runs/{run_id}/input/withdraw", dependencies=auth)
    async def withdraw_input(run_id: str, body: MessageBody) -> dict[str, bool]:
        """Take back a queued message; ``withdrawn`` is false once the model has read it."""
        await owned_run(run_id)
        return {"withdrawn": await application.withdraw_input(run_id, body.text)}

    @api.post("/runs/{run_id}/cancel", dependencies=auth)
    async def cancel(run_id: str) -> dict[str, Any]:
        """Stop a run and every subagent it started, so nothing keeps calling the model.

        Returns each affected run's final status; callers can confirm nothing is still going.
        """
        await owned_run(run_id)
        stopped: dict[str, str] = {}
        pending = [run_id]
        while pending:  # children first would race their parent's tool; any order is safe
            current = pending.pop()
            for child in await application.child_runs(current):
                pending.append(child.id)
            await application.cancel_run(current)
            final = await application.get_run(current)
            stopped[current] = str(final.status) if final else "unknown"
        return {"runs": stopped}

    @api.post("/runs/{run_id}/approvals/{approval_id}", dependencies=auth)
    async def decide(run_id: str, approval_id: str, body: ApprovalBody) -> dict[str, bool]:
        run = await owned_run(run_id)
        try:
            application.decide_approval(
                approval_id,
                run_id=run_id,
                session_id=run.session_id,
                principal=principal,
                approve=body.approve,
            )
        except Exception as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)[:200]) from None
        return {"approved": body.approve}

    @api.get("/runs/{run_id}/events", dependencies=auth)
    async def run_events(
        run_id: str,
        last_event_id: Annotated[str | None, Header()] = None,
        last_event_sequence: Annotated[int | None, Query(ge=0)] = None,
    ) -> StreamingResponse:
        await owned_run(run_id)
        after = last_event_sequence
        if after is None and last_event_id is not None and last_event_id.isdigit():
            after = int(last_event_id)
        return StreamingResponse(
            _stream(application, run_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return api


async def _stream(application: Application, run_id: str, after: int | None) -> AsyncIterator[str]:
    """Yield the run's logged events, until it ends; ``after`` resumes past a sequence.

    Sequences start at 0, so ``None`` (a fresh stream) means "from the very first event".
    """
    seen, quiet = after, 0.0
    while True:
        events = await application.replay(run_id=run_id, after_sequence=seen)
        for event in events:
            seen = event.sequence
            yield _sse(event)
        if any(e.type in _TERMINAL_EVENTS for e in events):
            return
        run = await application.get_run(run_id)
        if run is not None and str(run.status) in _TERMINAL_STATUSES and not events:
            # A hard cancel publishes no terminal event; the run store is the authority.
            tail = await application.replay(run_id=run_id, after_sequence=seen)
            for event in tail:
                seen = event.sequence
                yield _sse(event)
            return
        if events:
            quiet = 0.0
        else:
            quiet += _POLL_SECONDS
            if quiet >= _KEEPALIVE_SECONDS:
                quiet = 0.0
                yield ": keepalive\n\n"
        await asyncio.sleep(_POLL_SECONDS)


def run_web(application: Application, settings: WebSettings) -> None:
    """Serve the API (blocking; Ctrl+C to stop)."""
    import uvicorn

    uvicorn.run(
        create_app(application, settings),
        host=settings.host,
        port=settings.port,
        log_level="warning",
    )
