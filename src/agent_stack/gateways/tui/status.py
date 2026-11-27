"""What the chrome shows about the tabs: tab-bar labels, the sidebar, the prompt placeholder.

A 0.1 s tick moves spinners and durations; everything else repaints on change through
:meth:`StatusMixin._refresh_all`.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING

from textual.app import App

from agent_stack.gateways.base import usage_summary
from agent_stack.gateways.tui.chrome import PromptArea, Sidebar, SidebarState
from agent_stack.gateways.tui.rows import format_duration, spinner_frame
from agent_stack.gateways.tui.tabs import TabBar, ellipsize

if TYPE_CHECKING:
    from agent_stack.app.application import Application
    from agent_stack.gateways.tui.tabs import Conversation

#: Columns a tab's title gets in the tab bar before it ends in "...".
_TAB_TITLE = 24


class StatusMixin(App[None]):
    """Part of :class:`~agent_stack.gateways.tui.app.TuiApp`: tab bar, sidebar and prompt state."""

    if TYPE_CHECKING:  # from the rest of TuiApp
        application: Application
        tab: Conversation
        _tabs: list[Conversation]
        _branch: str

    def _tick(self) -> None:
        now = time.monotonic()
        for tab in self._tabs:
            for card in tab.cards.values():
                card.tick(now)
            for row in tab.subagent_rows.values():
                row.tick(now)
        if any(tab.running for tab in self._tabs):
            self._refresh_tabs(now)
        if self.tab.running:
            self._refresh_sidebar(now)

    def _refresh_all(self) -> None:
        if len(self.query(TabBar)) == 0:
            return  # fires in the gap between screen teardown and app unmount
        self._refresh_tabs()
        self._refresh_sidebar()
        self._refresh_prompt()

    def _refresh_tabs(self, now: float | None = None) -> None:
        bars = self.query(TabBar)
        if not bars:
            return
        now = time.monotonic() if now is None else now
        labels = []
        for tab in self._tabs:
            if tab.running:
                mark = "? " if tab.awaiting else f"{spinner_frame(now)} "
            else:
                mark = "✗ " if tab.outcome == "failed" else ""
            labels.append(mark + ellipsize(tab.title or "New conversation", _TAB_TITLE))
        bars.first(TabBar).show(labels, self._tabs.index(self.tab))

    def _status(self, now: float | None = None) -> str:
        """This tab's run state, as markup: working (with time), waiting, done, failed…"""
        tab = self.tab
        now = time.monotonic() if now is None else now
        if tab.running and tab.cancel_sent:
            return "[$warning]cancelling…[/]"
        if tab.running and tab.awaiting:
            return "[$warning]needs your approval[/]"
        if tab.running:
            return (
                f"[$accent]{spinner_frame(now)} working {format_duration(now - tab.run_started)}[/]"
            )
        if tab.outcome == "done":
            return f"[$success]✓ done in {format_duration(tab.elapsed)}[/]"
        if tab.outcome == "cancelled":
            return "[$warning]■ cancelled[/]"
        if tab.outcome == "failed":
            return "[$error]✗ failed[/]"
        return "[$text-muted]idle[/]"

    def _sidebar_state(self, now: float | None = None) -> SidebarState:
        tab = self.tab
        running = sum(1 for c in tab.cards.values() if c.status == "running")
        return SidebarState(
            title=tab.title or "New conversation",
            session=f"session {tab.session_id}" if tab.session_id else "not saved yet",
            tokens=tab.context or tab.tokens,
            estimated=not tab.context,
            window=self.application.config.context_window,
            tools_run=tab.tools_run,
            tools_running=running,
            subagents=[(r.agent_name, r.outcome) for r in tab.subagent_rows.values()],
            workspace=_short_path(self.application.config.workspace, 28),
            branch=self._branch,
            state=self._status(now),
            cost=usage_summary(tab.usage, context=False) if tab.usage.calls else "",
            mode=self.application.get_approval_mode(),
            model=self.application.get_model(),
        )

    def _refresh_sidebar(self, now: float | None = None) -> None:
        if len(self.query("#sidebar")) == 0:
            return
        self.query_one(Sidebar).render_state(self._sidebar_state(now))

    def _refresh_prompt(self) -> None:
        prompt = self.query_one("#prompt", PromptArea)
        prompt.placeholder = (
            "Working… type to queue a message (up arrow takes the last one back)"
            if self.tab.running
            else "Ask anything…"
        )


def _short(text: str, width: int) -> str:
    """Truncate `text` to `width` columns with a trailing ellipsis."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _short_path(path: Path, width: int) -> str:
    """Render a workspace path with a ``~/`` prefix when it sits under home."""
    try:
        rel = path.expanduser().resolve().relative_to(Path.home())
        text = "~/" + str(rel)
    except ValueError:
        text = str(path)
    return _short(text, width)
