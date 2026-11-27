"""Runs: start one, read its event stream, and render it into the tab that started it.

Every run's events go to the :class:`Conversation` that started it, shown or not, so a tab keeps
working while another one is on screen. Approval questions, tool cards (with a diff once a file
tool finishes), subagent rows and queued messages the model has read all land here; so does the
end of a run, which settles cards, hands unread queued messages back and syncs cost and title.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from textual.app import App

from agent_stack.core.events import AgentEvent, EventType
from agent_stack.gateways.base import Image, MessageEnvelope
from agent_stack.gateways.tui.chrome import PromptArea, Welcome
from agent_stack.gateways.tui.render import tool_args_lines, tool_detail
from agent_stack.gateways.tui.rows import AgentMessage, Disclosure, Notice, SubagentRow, UserMessage
from agent_stack.gateways.tui.screens import ApprovalScreen
from agent_stack.gateways.tui.subagent import SubagentScreen
from agent_stack.gateways.tui.tabs import Conversation

if TYPE_CHECKING:
    from agent_stack.app.application import Application

#: Tools whose calls change files; their cards show a diff once they finish.
_FILE_TOOLS = frozenset({"write_file", "edit_file"})


class RunsMixin(App[None]):
    """Part of :class:`~agent_stack.gateways.tui.app.TuiApp`: runs and their events."""

    if TYPE_CHECKING:  # from the rest of TuiApp
        application: Application
        tab: Conversation
        _principal: str

        def _spawn(self, coro: Any) -> None: ...
        def _show_tab(self, tab: Conversation) -> None: ...
        def _refresh_all(self) -> None: ...
        def _refresh_sidebar(self, now: float | None = None) -> None: ...

    async def _start_run(
        self, tab: Conversation, text: str, images: tuple[Image, ...] = ()
    ) -> None:
        envelope = MessageEnvelope(
            principal=self._principal, session=tab.session_id or "", text=text, images=images
        )
        handle = await self.application.send_message(envelope)
        tab.run_id = handle.run_id
        tab.running = True
        tab.cancel_sent = False
        tab.run_started = time.monotonic()
        self._refresh_all()
        self._spawn(self._consume(tab, handle))

    async def _consume(self, tab: Conversation, handle: Any) -> None:
        try:
            async for event in handle.events():
                self.handle_event(event, tab)
            if tab.running and tab.run_id == handle.run_id:
                # The stream ended with no terminal event: a hard cancel publishes none.
                self._finish_run(tab, "failed")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken run must not tear down the UI
            self.notify(str(exc), severity="error")
            self._finish_run(tab, "failed")

    # ── event routing ─────────────────────────────────────────────────────────
    def handle_event(self, event: AgentEvent, tab: Conversation | None = None) -> None:
        """Route one runtime event to its tab's conversation, the tab bar and the sidebar."""
        tab = tab or self.tab
        if not tab.is_attached:
            # The tab was closed, or the screen torn down (quit or test teardown), while a
            # stream was still live; drop the event rather than touching dead widgets.
            return
        if event.type is EventType.MODEL_USAGE:
            # Every run counts, a subagent's included: they are all this conversation's spend.
            tab.usage = tab.usage.plus(event.data)
            if event.run_id == tab.run_id and event.data.get("purpose") != "title":
                tab.context = int(event.data.get("input_tokens", 0) or 0) or tab.context
            self._refresh_all()
            return
        if event.type is EventType.APPROVAL_REQUIRED:
            self._on_approval_required(tab, event)  # from any run: a child can be blocked too
        elif event.type is EventType.APPROVAL_RESOLVED:
            self._on_approval_resolved(tab, event)
        elif event.type is EventType.APPROVAL_AUTO:
            self._on_approval_auto(event)
        elif event.type is EventType.SUBAGENT_STARTED:
            self._on_subagent_started(tab, event)
        elif event.type is EventType.SUBAGENT_COMPLETED:
            self._on_subagent_completed(tab, event)
        elif event.type is EventType.CONTEXT_COMPACTED and event.parent_run_id is None:
            tab.mount(
                Notice(f"context compacted: {event.data.get('messages', 0)} messages summarised")
            )
        elif event.type is EventType.MODEL_RETRY and event.parent_run_id is None:
            tab.mount(Notice(str(event.data.get("text", "retrying the model call"))))
        elif event.parent_run_id is not None and event.run_id != tab.run_id:
            pass  # child-run detail stays out of the parent conversation
        else:
            self._on_main_event(tab, event)

    def _on_main_event(self, tab: Conversation, event: AgentEvent) -> None:
        etype = event.type
        if etype is EventType.RUN_STARTED:
            tab.run_id = event.run_id
            if not tab.running:
                tab.running = True
                tab.run_started = time.monotonic()
        elif etype is EventType.MODEL_REASONING:
            self._on_reasoning(tab, event)
        elif etype is EventType.ASSISTANT_DELTA:
            self._on_delta(tab, event)
            return  # the message repaints itself; no need to refresh the chrome per piece
        elif etype is EventType.ASSISTANT_MESSAGE:
            self._on_assistant(tab, event)
        elif etype is EventType.TOOL_STARTED:
            self._on_tool_started(tab, event)
        elif etype is EventType.TOOL_OUTPUT:
            card = tab.cards.get(str(event.data.get("tool_call_id")))
            if card is not None:
                card.append(str(event.data.get("chunk", "")))
        elif etype is EventType.TOOL_COMPLETED:
            call_id = str(event.data.get("tool_call_id"))
            card = tab.cards.get(call_id)
            if card is not None:
                ok = event.data.get("ok", True)  # older events carry no flag
                if ok and call_id in tab.file_paths:
                    # settle after the diff arrives, so the card opens on what changed
                    self._spawn(self._finish_with_diff(tab, card, call_id, event))
                else:
                    card.finish("done" if ok else "failed")
        elif etype is EventType.INPUT_RECEIVED:
            self._on_input_received(tab, str(event.data.get("text", "")))
        elif etype is EventType.RUN_FAILED:
            self._on_failed(tab, event)
        elif etype is EventType.RUN_COMPLETED:
            self._finish_run(tab, "done")  # a successful run adds no row to the conversation
        if etype is not EventType.TOOL_OUTPUT:  # output only moves the card; the tick repaints it
            self._refresh_all()

    def _on_approval_required(self, tab: Conversation, event: AgentEvent) -> None:
        """Pause on the card and ask the human; the answer goes back through the Application."""
        call_id = str(event.data.get("tool_call_id", ""))
        tool = str(event.data.get("tool", "tool"))
        args = event.data.get("args")
        card = tab.cards.get(call_id)
        if card is not None:
            card.await_approval()
        tab.awaiting += 1
        detail = tool_detail(args) if tool == "run_command" else tool_args_lines(args)
        approval_id = str(event.data.get("approval_id", ""))

        def answer(approved: bool | None) -> None:
            if screen in tab.dialogs:
                tab.dialogs.remove(screen)
            if not tab.running:
                return  # the run ended (cancelled) while the dialog was open
            try:
                self.application.decide_approval(
                    approval_id,
                    run_id=event.run_id,
                    session_id=event.session_id,
                    principal=self._principal,
                    approve=bool(approved),
                )
            except Exception as exc:  # a refused decision must not take the UI down
                self.notify(str(exc), severity="warning")

        if tab is not self.tab:
            self._show_tab(tab)  # what is being approved is on screen behind the question
        screen = ApprovalScreen(tool, detail, str(event.data.get("note", "")))
        tab.dialogs.append(screen)
        self.push_screen(screen, answer)
        self._refresh_all()

    def _on_approval_auto(self, event: AgentEvent) -> None:
        """Say what auto mode decided, so nothing is approved or blocked silently."""
        verdict = str(event.data.get("verdict"))
        tool = str(event.data.get("tool", "tool"))
        reason = str(event.data.get("reason", ""))
        if verdict == "allow":
            self.notify(f"auto-approved {tool}: {reason}", timeout=3)
        elif verdict == "deny":
            self.notify(f"auto mode blocked {tool}: {reason}", severity="error", timeout=10)
        # "ask" is followed by the usual question, which shows the reason

    def _on_approval_resolved(self, tab: Conversation, event: AgentEvent) -> None:
        tab.awaiting = max(0, tab.awaiting - 1)
        card = tab.cards.get(str(event.data.get("tool_call_id", "")))
        if card is not None and event.data.get("status") == "approved":
            card.resume_running()
        self._refresh_all()

    def _close_approval_dialogs(self, tab: Conversation) -> None:
        """Dismiss the tab's open approval dialogs (the run they belonged to is over)."""
        while self.screen in tab.dialogs:
            self.screen.dismiss(False)
        tab.dialogs.clear()

    def _on_reasoning(self, tab: Conversation, event: AgentEvent) -> None:
        text = str(event.data.get("text", ""))
        if not text.strip():
            return
        self._bump_tokens(tab, text)
        self._remove_welcome(tab)
        # the step's reply may already be streaming: its thinking goes above it
        tab.mount(Disclosure("think", "thinking", body=text), before=tab.streaming)

    def _on_delta(self, tab: Conversation, event: AgentEvent) -> None:
        """Stream the reply's text into one message; its thinking arrives whole, before it."""
        text = str(event.data.get("text", ""))
        if event.data.get("kind") != "text" or not text:
            return
        if tab.streaming is None:
            self._remove_welcome(tab)
            tab.streaming = AgentMessage("")
            tab.mount(tab.streaming)
        tab.streaming.grow(text)

    def _on_assistant(self, tab: Conversation, event: AgentEvent) -> None:
        content = str(event.data.get("content", ""))
        streamed, tab.streaming = tab.streaming, None
        if not content.strip():
            if streamed is not None:
                streamed.remove()
            return
        if streamed is not None:
            self._bump_tokens(tab, content)
            streamed.settle(content)
            return
        self._bump_tokens(tab, content)
        self._remove_welcome(tab)
        tab.mount(AgentMessage(content))

    def _on_tool_started(self, tab: Conversation, event: AgentEvent) -> None:
        name = str(event.data.get("tool", "tool"))
        args = event.data.get("args")
        card = Disclosure(
            "tool",
            name,
            tool_detail(args),
            body=tool_args_lines(args),
            running=True,
            opened=name == "run_command",
        )
        self._remove_welcome(tab)
        tab.mount(card)
        tab.tools_run += 1
        call_id = str(event.data.get("tool_call_id", ""))
        if call_id:
            tab.cards[call_id] = card
            path = args.get("path") if isinstance(args, Mapping) else None
            if name in _FILE_TOOLS and isinstance(path, str):
                tab.file_paths[call_id] = path

    def _on_failed(self, tab: Conversation, event: AgentEvent) -> None:
        tab.mount(Notice(f"error: {event.data.get('error', 'run failed')}"))
        self._finish_run(tab, "failed")

    def _on_subagent_started(self, tab: Conversation, event: AgentEvent) -> None:
        child = str(event.data.get("child_run_id", ""))
        name = str(event.data.get("agent_name", "subagent"))
        if child:
            tab.last_child_id = child
        row = SubagentRow(name, run_id=child, task=str(event.data.get("task", "")))
        tab.mount(row)
        if child:
            tab.subagent_rows[child] = row
        self._refresh_sidebar()

    def on_subagent_row_open(self, message: SubagentRow.Open) -> None:
        """Show what a subagent is doing (or did), from its row here or in another subagent."""
        self.push_screen(
            SubagentScreen(self.application, message.run_id, message.agent, message.brief)
        )

    def _on_subagent_completed(self, tab: Conversation, event: AgentEvent) -> None:
        child = str(event.data.get("child_run_id", ""))
        status = str(event.data.get("status", "completed"))
        row = tab.subagent_rows.get(child)
        if row is not None:
            row.finish(status)
        self._refresh_sidebar()

    def _on_input_received(self, tab: Conversation, text: str) -> None:
        """A queued message reached the model: show it where it was read."""
        bubble = next((b for b in tab.queued if b.text == text), None)
        if bubble is None:
            return
        tab.queued.remove(bubble)
        bubble.remove()
        tab.mount(UserMessage(text))

    def _finish_run(self, tab: Conversation, outcome: str) -> None:
        if not tab.running and outcome == "failed":
            return
        if tab.queued:  # a stopped run never read them: hand them back to edit or resend
            text = "\n\n".join(b.text for b in tab.queued)
            if tab is self.tab:
                prompt = self.query_one("#prompt", PromptArea)
                prompt.load_text(f"{text}\n\n{prompt.text}".strip())
            else:
                tab.draft = f"{text}\n\n{tab.draft}".strip()
            for bubble in tab.queued:
                bubble.remove()
            tab.queued.clear()
        self._close_approval_dialogs(tab)
        tab.awaiting = 0
        tab.elapsed = time.monotonic() - tab.run_started if tab.run_started else 0.0
        tab.outcome = "cancelled" if tab.cancel_sent and outcome == "failed" else outcome
        tab.running = False
        tab.cancel_sent = False
        tab.streaming = None  # a stopped stream keeps the text it got
        for card in tab.cards.values():
            if card.status == "running":
                card.finish("done")
            elif card.status == "waiting":  # the run ended while it was asking
                card.finish("failed")
        self._refresh_all()
        self._spawn(self._sync_usage(tab))
        if not tab.named:
            self._spawn(self._sync_title(tab))

    async def _finish_with_diff(
        self, tab: Conversation, card: Disclosure, call_id: str, event: AgentEvent
    ) -> None:
        """Fill a write/edit card with the diff of what that call changed, then settle it."""
        path = tab.file_paths.pop(call_id, "")
        try:
            change = await self.application.changed_file(
                event.session_id, path, event.run_id, latest_only=True
            )
        except Exception:  # a diff is a nicety: the card must settle either way
            change = None
        if change is not None:
            card.show_diff(change.diff or "(no net change)")
        card.finish("done")
        self._refresh_all()

    async def _sync_usage(self, tab: Conversation) -> None:
        """Replace the running total with the durable one (it also holds subagent runs)."""
        session_id = tab.session_id
        if session_id is None:
            return
        try:
            usage = await self.application.session_usage(session_id)
        except Exception:  # a cost display must never break the conversation
            return
        if tab.session_id == session_id:
            tab.usage = usage
            self._refresh_all()

    async def _sync_title(self, tab: Conversation, tries: int = 5) -> None:
        """Pick up the title the small model gave the conversation (it lands in the background)."""
        session_id = tab.session_id
        for attempt in range(tries):
            if session_id is None or tab.session_id != session_id:
                return
            with contextlib.suppress(Exception):  # a title is cosmetic
                session = await self.application.get_session(session_id)
                title = session.title if session is not None else ""
                if title and title != (tab.title or "")[:80]:
                    tab.title, tab.named = title, True
                    self._refresh_all()
                    return
            if attempt + 1 < tries:
                await asyncio.sleep(2)

    # ── conversation helpers ──────────────────────────────────────────────────
    def _remove_welcome(self, tab: Conversation) -> None:
        """First content ends the empty state and starts following the output.

        The chat is anchored only once there is a conversation: an anchored container
        bottom-aligns short content, which would push the welcome panel to the bottom.
        """
        for welcome in tab.query(Welcome):
            welcome.remove()
        if not tab.is_anchored:
            tab.anchor()  # follow new output unless the user scrolls up

    def _bump_tokens(self, tab: Conversation, text: str) -> None:
        """Approximate context usage until the model reports it: ~4 chars per token."""
        tab.tokens += max(1, len(text) // 4)
        self._refresh_sidebar()
