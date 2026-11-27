"""Tabs and stored conversations: open, show, close, switch, pick, delete.

Each open conversation is a :class:`Conversation` tab in the ``#chats`` switcher; the prompt keeps
each tab's unsent text. Opening a stored conversation shows its tab if one is open, else loads its
history into a tab (an empty one is reused).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from textual.app import App
from textual.widgets import ContentSwitcher

from agent_stack.gateways.tui.chrome import PromptArea
from agent_stack.gateways.tui.render import tool_args_lines, tool_detail
from agent_stack.gateways.tui.rows import AgentMessage, Disclosure, SubagentRow, UserMessage
from agent_stack.gateways.tui.screens import SessionPicker
from agent_stack.gateways.tui.subagent import delegated_run
from agent_stack.gateways.tui.tabs import Conversation, TabBar

if TYPE_CHECKING:
    from agent_stack.app.application import Application


class SessionsMixin(App[None]):
    """Part of :class:`~agent_stack.gateways.tui.app.TuiApp`: tabs and stored conversations."""

    if TYPE_CHECKING:  # from the rest of TuiApp
        application: Application
        tab: Conversation
        _tabs: list[Conversation]
        _tab_seq: int
        _titles: dict[str, str]
        _principal: str

        def _spawn(self, coro: Any) -> None: ...
        def _busy(self) -> bool: ...
        def _remove_welcome(self, tab: Conversation) -> None: ...
        def _refresh_all(self) -> None: ...

    async def _resume_session(self, session_id: str) -> None:
        session = await self.application.get_session(session_id)
        if session is None:
            self.notify(f"No conversation {session_id}.", severity="error")
            self.tab.session_id = None
            return
        self._titles[session_id] = session.title or ""
        await self._open_session(session_id, here=True)

    def _new_conversation(self) -> Conversation:
        self._tab_seq += 1
        return Conversation(id=f"chat-{self._tab_seq}")

    def _show_tab(self, tab: Conversation) -> None:
        """Put ``tab`` on screen; the prompt keeps each tab's unsent text."""
        prompt = self.query_one("#prompt", PromptArea)
        if tab is not self.tab:
            self.tab.draft = prompt.text
            prompt.load_text(tab.draft)
            prompt.move_cursor(prompt.document.end)
        self.tab = tab
        self.query_one("#chats", ContentSwitcher).current = tab.id
        self._refresh_all()
        prompt.focus()

    async def _add_tab(self) -> Conversation:
        tab = self._new_conversation()
        tab.display = False  # the switcher shows it once it is current
        await self.query_one("#chats", ContentSwitcher).mount(tab)
        self._tabs.append(tab)
        self._show_tab(tab)
        return tab

    async def _replace_tab(self, old: Conversation) -> Conversation:
        """A fresh, empty pane in ``old``'s place (the stored conversation is untouched)."""
        tab = self._new_conversation()
        tab.display = False
        await self.query_one("#chats", ContentSwitcher).mount(tab, after=old)
        self._tabs[self._tabs.index(old)] = tab
        self._show_tab(tab)
        await old.remove()
        return tab

    async def action_new_session(self) -> None:
        """Open a new tab with a fresh conversation (stored when its first message is sent)."""
        if self.tab.blank:
            self.query_one("#prompt", PromptArea).focus()
            return
        await self._add_tab()

    async def action_close_tab(self) -> None:
        """Close this tab (``exit`` or ``/close``); closing the last one leaves the app.

        The conversation stays stored and can be reopened (ctrl+o).
        """
        if self._busy():
            return
        old = self.tab
        if len(self._tabs) == 1:
            self.exit()
            return
        at = self._tabs.index(old)
        self._tabs.remove(old)
        self._show_tab(self._tabs[min(at, len(self._tabs) - 1)])
        await old.remove()

    def action_go_to_tab(self, index: int) -> None:
        if 0 <= index < len(self._tabs):
            self._show_tab(self._tabs[index])

    def action_next_tab(self) -> None:
        self.action_go_to_tab((self._tabs.index(self.tab) + 1) % len(self._tabs))

    def action_previous_tab(self) -> None:
        self.action_go_to_tab((self._tabs.index(self.tab) - 1) % len(self._tabs))

    async def on_tab_bar_picked(self, message: TabBar.Picked) -> None:
        if message.index < 0:
            await self.action_new_session()
        else:
            self.action_go_to_tab(message.index)

    async def _warn_if_files_differ(self, session_id: str) -> None:
        """After opening a conversation: say so when the folder is in another one's state."""
        try:
            differ = await self.application.timeline.files_differ(session_id)
        except Exception:
            return
        if differ:
            self.notify(
                "The files in the folder match another conversation. "
                "ctrl+p, then 'Put this conversation's files in the folder' to match this one.",
                severity="warning",
                timeout=10,
            )

    async def action_switch_session(self) -> None:
        """Open the conversation picker."""
        sessions = await self.application.list_sessions(self._principal)
        self._titles = {s.id: s.title for s in sessions}
        self.push_screen(
            SessionPicker(sessions, self.tab.session_id, self._delete_session),
            self._on_session_picked,
        )

    async def _delete_session(self, session_id: str) -> None:
        """Delete a stored conversation; a tab showing it closes (or empties, if it is the last)."""
        tab = next((t for t in self._tabs if t.session_id == session_id), None)
        if tab is not None and tab.running:
            self.notify("That conversation is running. Cancel it first.", severity="warning")
            return
        await self.application.delete_session(session_id)
        self._titles.pop(session_id, None)
        if tab is not None:
            if len(self._tabs) == 1:
                await self._replace_tab(tab)
            else:
                self._tabs.remove(tab)
                if tab is self.tab:
                    self._show_tab(self._tabs[0])
                await tab.remove()
                self._refresh_all()
        self.notify("Conversation deleted.")

    def _on_session_picked(self, session_id: str | None) -> None:
        if session_id is not None and session_id != self.tab.session_id:
            self._spawn(self._open_session(session_id))

    async def _open_session(self, session_id: str, *, here: bool = False) -> None:
        """Show a stored conversation: its tab if one is open, else a tab with its history.

        ``here`` loads it into this tab (a rewind's branch, the conversation to resume);
        otherwise an empty tab is reused and anything else stays open in its own tab.
        """
        existing = next((t for t in self._tabs if t.session_id == session_id), None)
        if existing is not None and not here:
            self._show_tab(existing)
            return
        history = await self.application.session_history(session_id)
        tab = await (self._replace_tab(self.tab) if here or self.tab.blank else self._add_tab())
        tab.session_id = session_id
        with contextlib.suppress(Exception):  # the cost is informational; history matters more
            tab.usage = await self.application.session_usage(session_id)
            tab.context = tab.usage.last_input_tokens
        title = self._titles.get(session_id, "")
        tab.title = None if title in ("", "Untitled") else title
        tab.named = tab.title is not None
        if history:
            self._remove_welcome(tab)
        for item in history:
            if item.kind == "user":
                tab.mount(UserMessage(item.text, images=item.images))
            elif item.kind == "assistant":
                tab.mount(AgentMessage(item.text))
            elif item.kind == "compaction":
                tab.mount(Disclosure("think", "context compacted", "summary above", body=item.text))
            elif item.kind == "tool":
                tab.mount(
                    Disclosure(
                        "tool",
                        item.text,
                        tool_detail(item.args),
                        body=tool_args_lines(item.args),
                    )
                )
                child = delegated_run(item.result) if item.text == "delegate" else None
                if child is not None:  # the stored subagent opens like a live one
                    args = item.args or {}
                    name, task = str(args.get("agent_name", "subagent")), str(args.get("task", ""))
                    tab.mount(SubagentRow(name, run_id=child, task=task, done=True))
            tab.tokens += max(1, len(item.text) // 4)
        self._refresh_all()
        self.call_after_refresh(tab.scroll_end, animate=False)
        self.query_one("#prompt", PromptArea).focus()
        await self._warn_if_files_differ(session_id)
