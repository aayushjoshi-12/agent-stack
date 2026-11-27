"""Tabs: each open conversation is a :class:`Conversation` pane with its own run state.

A tab keeps its run going while another tab is shown: the app routes each run's events to the
pane that started it, and the :class:`TabBar` across the top shows every tab's title and
whether it is working.
"""

from __future__ import annotations

from textual.containers import Horizontal, VerticalScroll
from textual.events import Click
from textual.markup import escape
from textual.message import Message
from textual.widgets import Static

from agent_stack.gateways.base import SessionUsage
from agent_stack.gateways.tui.chrome import Welcome
from agent_stack.gateways.tui.rows import Disclosure, SubagentRow, UserMessage
from agent_stack.gateways.tui.screens import ApprovalScreen

__all__ = ["Conversation", "TabBar"]


class Conversation(VerticalScroll):
    """One tab: the chat pane plus everything about the conversation shown in it."""

    def __init__(self, *, id: str) -> None:
        super().__init__(Welcome(), id=id, classes="chat")
        self.session_id: str | None = None
        self.title: str | None = None
        self.named = False  # the title is the stored one, not the first message standing in
        self.run_id: str | None = None
        self.running = False
        self.cancel_sent = False
        self.run_started = 0.0
        self.outcome = ""  # "" | "done" | "failed" | "cancelled"
        self.elapsed = 0.0
        self.awaiting = 0  # approval requests currently open
        self.dialogs: list[ApprovalScreen] = []
        self.queued: list[UserMessage] = []  # sent mid-run, not read by the model yet
        self.tokens = 0  # rough estimate (4 chars a token) until the model reports a size
        self.context = 0  # input tokens of this conversation's last main model call
        self.usage = SessionUsage()  # what this conversation has cost so far (all runs)
        self.cards: dict[str, Disclosure] = {}
        self.subagent_rows: dict[str, SubagentRow] = {}
        self.tools_run = 0
        self.file_paths: dict[str, str] = {}  # write/edit call id -> the path it touches
        self.last_child_id: str | None = None
        self.draft = ""  # the prompt's text while another tab is shown

    @property
    def blank(self) -> bool:
        """Nothing shown here yet: opening a conversation may reuse this tab."""
        return self.title is None and not self.tokens and not self.running


class _Tab(Static):
    def __init__(self, index: int) -> None:
        super().__init__("", classes="tab")
        self.index = index

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(TabBar.Picked(self.index))


class _NewTab(Static):
    def __init__(self) -> None:
        super().__init__("+", classes="tab new-tab")

    def on_click(self, event: Click) -> None:
        event.stop()
        self.post_message(TabBar.Picked(-1))


class TabBar(Horizontal):
    """The top bar: one tab per open conversation, then ``+`` for a new one."""

    class Picked(Message):
        """A tab was clicked (``index``), or ``+`` (``-1``)."""

        def __init__(self, index: int) -> None:
            super().__init__()
            self.index = index

    def show(self, labels: list[str], current: int) -> None:
        tabs = list(self.query(_Tab))
        if len(tabs) != len(labels):
            self.remove_children()
            tabs = [_Tab(i) for i in range(len(labels))]
            self.mount_all([*tabs, _NewTab()])
        for i, (tab, label) in enumerate(zip(tabs, labels, strict=True)):
            tab.update(escape(label))
            tab.set_class(i == current, "-current")


def ellipsize(text: str, width: int) -> str:
    """``text`` on one line, cut to ``width`` with ``...`` at the end when longer."""
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 3].rstrip() + "..."
