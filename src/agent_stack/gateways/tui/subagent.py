"""A subagent's own run, opened from its row: tool calls, reasoning and replies (P48).

The conversation shows a subagent as one :class:`~rows.SubagentRow`; Enter or a click on it
opens :class:`SubagentScreen`. The screen reads the child run from the durable event log
(``Application.replay``) and keeps polling while it runs, so a running and a finished subagent
look the same, and so does one in a reopened conversation.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, ClassVar

from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Static

from agent_stack.core.events import AgentEvent, EventType
from agent_stack.gateways.tui.render import tool_args_lines, tool_detail
from agent_stack.gateways.tui.rows import (
    AgentMessage,
    Disclosure,
    Notice,
    SubagentRow,
    format_duration,
)

if TYPE_CHECKING:
    from agent_stack.app.application import Application

__all__ = ["SubagentScreen", "delegated_run"]

_POLL_SECONDS = 0.5
_TERMINAL = frozenset({"completed", "failed", "cancelled"})
#: The event log's mark on a field it shortened (``observability/audit.py`` ``_cap``).
_CAPPED = re.compile(r"\.\.\.\[truncated \d+ bytes\]$")


def delegated_run(result: str | None) -> str | None:
    """The run id a stored ``delegate`` result names (``... (run <id>) ...``), if any."""
    if not result:
        return None
    _, found, rest = result.partition("(run ")
    run_id = rest.split(")", 1)[0]
    return run_id if found and len(run_id) == 32 and run_id.isalnum() else None


class SubagentScreen(ModalScreen[None]):
    """One subagent's run as it happens (or happened); Esc closes."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape,q", "close", "close")]

    def __init__(self, application: Application, run_id: str, agent: str, task: str) -> None:
        super().__init__()
        self._application = application
        self._run_id = run_id
        self._agent = agent
        self._brief = task
        self._seen: int | None = None
        self._status = "running"
        self._t0 = time.monotonic()
        self._tools = 0
        self._cards: dict[str, Disclosure] = {}
        self._rows: dict[str, SubagentRow] = {}
        self._capped: list[AgentMessage] = []  # replies the event log cut short
        self._session_id = ""

    def compose(self):  # type: ignore[no-untyped-def]
        with Vertical(id="subagent-card"):
            yield Static("", id="subagent-title")
            if self._brief:
                task = " ".join(self._brief.split())
                yield Static(
                    f"[$foreground-muted]task: {escape(task[:400])}[/]", id="subagent-task"
                )
            yield VerticalScroll(id="subagent-log")
            yield Static(
                "[$foreground-muted]esc close · enter on a card opens it[/]", id="picker-foot"
            )

    async def on_mount(self) -> None:
        self._paint()
        await self._poll()
        self._timer = self.set_interval(_POLL_SECONDS, self._poll)
        self.set_interval(0.1, self._tick)

    async def _poll(self) -> None:
        if self._status in _TERMINAL:
            return
        try:
            events = await self._application.replay(run_id=self._run_id, after_sequence=self._seen)
        except Exception as exc:  # a broken log must not take the screen down
            self._log().mount(Notice(f"cannot read the subagent's events: {exc}"))
            self._status = "failed"
            return
        for event in events:
            self._seen = event.sequence
            self._session_id = event.session_id
            self._apply(event)
        if not events and self._status == "running":
            run = await self._application.get_run(self._run_id)
            if run is not None and str(run.status) in _TERMINAL:  # a hard cancel logs no end
                self._end(str(run.status))
        if self._status in _TERMINAL and self._capped:
            await self._uncap()
        if events:
            self._log().scroll_end(animate=False)
        self._paint()

    async def _uncap(self) -> None:
        """Show the whole of each reply the log cut short, from the subagent's own transcript."""
        try:
            history = await self._application.session_history(self._session_id)
        except Exception:  # the shortened text stays; it says it was cut
            return
        whole = [item.text for item in history if item.kind == "assistant"]
        for reply in self._capped:
            start = _CAPPED.sub("", reply.source)
            found = next((text for text in whole if text.startswith(start)), None)
            if found is not None:
                reply.settle(found)
        self._capped.clear()

    def _apply(self, event: AgentEvent) -> None:
        log, data, kind = self._log(), event.data, event.type
        if kind is EventType.TOOL_STARTED:
            args = data.get("args")
            card = Disclosure(
                "tool",
                str(data.get("tool", "tool")),
                tool_detail(args),
                body=tool_args_lines(args),
                running=True,
            )
            self._cards[str(data.get("tool_call_id", ""))] = card
            self._tools += 1
            log.mount(card)
        elif kind is EventType.TOOL_OUTPUT:
            if (out := self._cards.get(str(data.get("tool_call_id", "")))) is not None:
                out.append(str(data.get("chunk", "")))
        elif kind is EventType.TOOL_COMPLETED:
            if (done := self._cards.get(str(data.get("tool_call_id", "")))) is not None:
                done.finish("done" if data.get("ok", True) else "failed")
        elif kind is EventType.MODEL_REASONING and (text := str(data.get("text", ""))):
            log.mount(Disclosure("think", "thinking", body=text))
        elif kind is EventType.ASSISTANT_MESSAGE:
            if text := _text(data.get("content")):
                reply = AgentMessage(text)
                if _CAPPED.search(text):
                    self._capped.append(reply)
                log.mount(reply)
        elif kind is EventType.SUBAGENT_STARTED:
            child = str(data.get("child_run_id", ""))
            row = SubagentRow(
                str(data.get("agent_name", "subagent")),
                run_id=child,
                task=str(data.get("task", "")),
            )
            self._rows[child] = row
            log.mount(row)
        elif kind is EventType.SUBAGENT_COMPLETED:
            if (ended := self._rows.get(str(data.get("child_run_id", "")))) is not None:
                ended.finish(str(data.get("status", "completed")))
        elif kind is EventType.RUN_COMPLETED:
            self._end("completed")
        elif kind is EventType.RUN_FAILED:
            error = str(data.get("error", "failed"))
            cancelled = "RunCancelled" in error
            if not cancelled:
                log.mount(Notice(f"error: {error}"))
            self._end("cancelled" if cancelled else "failed")

    def _end(self, status: str) -> None:
        self._status = status
        for card in self._cards.values():
            if card.status == "running":
                card.finish("failed" if status != "completed" else "done")

    def _tick(self) -> None:
        now = time.monotonic()
        for card in self._cards.values():
            card.tick(now)
        for row in self._rows.values():
            row.tick(now)
        if self._status == "running":
            self._paint()

    def _paint(self) -> None:
        if self._status == "running":
            state = f"[$accent]running[/] {format_duration(time.monotonic() - self._t0)}"
        elif self._status == "completed":
            state = "[$success]done[/]"
        else:
            state = f"[$error]{self._status}[/]"
        tools = f"{self._tools} tool call{'' if self._tools == 1 else 's'}"
        self.query_one("#subagent-title", Static).update(
            f"[bold $primary]subagent · {escape(self._agent)}[/]  {state}"
            f"  [$foreground-muted]{tools}[/]"
        )

    def _log(self) -> VerticalScroll:
        return self.query_one("#subagent-log", VerticalScroll)

    def action_close(self) -> None:
        self.dismiss(None)


def _text(content: object) -> str:
    """An assistant message's text (content may be a string or a list of blocks)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict)]
        return "\n".join(p for p in parts if isinstance(p, str)).strip()
    return ""
