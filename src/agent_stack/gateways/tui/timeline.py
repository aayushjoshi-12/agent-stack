"""The conversation's timeline: rewind, branches, references, restoring files, changed files.

Everything here goes through ``Application.timeline`` (or ``changed_files``) and opens its result
as a conversation or a modal screen.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.app import App

from agent_stack.gateways.tui.chrome import PromptArea
from agent_stack.gateways.tui.screens import BranchesScreen, ChangesScreen, RewindScreen

if TYPE_CHECKING:
    from agent_stack.app.application import Application
    from agent_stack.gateways.tui.tabs import Conversation


class TimelineMixin(App[None]):
    """Part of :class:`~agent_stack.gateways.tui.app.TuiApp`: rewind, branches, references, files."""

    if TYPE_CHECKING:  # from the rest of TuiApp
        application: Application
        tab: Conversation
        _titles: dict[str, str]

        def _spawn(self, coro: Any) -> None: ...
        def _busy(self) -> bool: ...
        async def _open_session(self, session_id: str, *, here: bool = False) -> None: ...

    async def _open_reference(self, reference: str) -> None:
        """Open a point shared from another conversation as a new one."""
        try:
            opened = await self.application.timeline.open_reference(reference)
        except Exception as exc:
            self.notify(f"Cannot open that reference: {exc}", severity="error")
            return
        self._titles[opened] = "from a reference"
        await self._open_session(opened)  # also warns when the folder holds other files
        self.notify("Opened from a reference. Its history is above; continue from here.")

    async def action_show_branches(self) -> None:
        """Draw the branches of the open conversation."""
        if self.tab.session_id is None:
            self.notify("Nothing to show yet.")
            return
        try:
            text = await self.application.timeline.tree_text(self.tab.session_id)
        except Exception as exc:
            self.notify(f"Cannot draw the branches: {exc}", severity="error")
            return
        self.push_screen(BranchesScreen(text))

    async def action_make_reference(self) -> None:
        """Name the last finished turn so another conversation can open it."""
        session_id = self.tab.session_id
        if session_id is None:
            self.notify("Nothing to reference yet.")
            return
        try:
            timeline = self.application.timeline
            done = [t for t in await timeline.turns(session_id) if t.completed_event_id]
            if not done:
                self.notify("Nothing has finished in this conversation yet.")
                return
            reference = await timeline.reference(session_id, str(done[-1].index))
        except Exception as exc:
            self.notify(f"Cannot make a reference: {exc}", severity="error")
            return
        self.copy_to_clipboard(reference)
        self.notify(
            f"Copied. Paste it into another conversation's prompt and press enter:\n{reference}",
            timeout=15,
        )

    async def action_show_changes(self) -> None:
        """Open the list of files the agent created or edited in this conversation."""
        if self.tab.session_id is None:
            self.notify("Nothing has been changed yet.", severity="information")
            return
        try:
            files = await self.application.changed_files(self.tab.session_id)
        except Exception as exc:
            self.notify(f"Could not read the changes: {exc}", severity="error")
            return
        self.push_screen(ChangesScreen(files))

    async def action_rewind(self) -> None:
        """Pick an earlier message and go back to it (conversation, code, or both)."""
        if self._busy():
            return
        session_id = self.tab.session_id
        if session_id is None:
            self.notify("Nothing to rewind to: this conversation has no messages yet.")
            return
        try:
            turns = await self.application.timeline.lineage(session_id)
        except Exception as exc:  # a conversation with no event log, say
            self.notify(f"Cannot rewind here: {exc}", severity="error")
            return
        turns = [t for t in turns if t.turn.user_text]
        if not turns:
            self.notify(
                f"Nothing to rewind to: no earlier messages found for {session_id}.",
                severity="warning",
            )
            return
        self.push_screen(RewindScreen(turns), self._on_rewind_picked)

    async def action_restore_files(self) -> None:
        """Put the folder in this conversation's file state (after opening another one)."""
        if self._busy() or self.tab.session_id is None:
            return
        try:
            result = await self.application.timeline.restore_files(self.tab.session_id)
        except Exception as exc:
            self.notify(f"Could not restore files: {exc}", severity="error")
            return
        moved = len(result.moved)
        text = (
            f"{moved} file{'s' if moved != 1 else ''} restored."
            if moved
            else "Files already match."
        )
        if result.conflicts:
            text += f" Left alone (changed since): {', '.join(result.conflicts)}"
        self.notify(text, severity="warning" if result.conflicts else "information")

    def _on_rewind_picked(self, choice: tuple[Any, str] | None) -> None:
        if choice is not None:
            self._spawn(self._rewind(*choice))

    async def _rewind(self, target: Any, mode: str) -> None:
        current = self.tab.session_id
        if current is None:
            return
        try:
            outcome = await self.application.timeline.rewind_to(
                current,
                target,
                conversation=mode in ("both", "conversation"),
                code=mode in ("both", "code"),
            )
        except Exception as exc:
            self.notify(f"Rewind failed: {exc}", severity="error")
            return
        if outcome.session_id != current:
            self._titles[outcome.session_id] = "rewound"
            await self._open_session(outcome.session_id, here=True)
        moved = len(outcome.files.moved)
        text = (
            f"Rewound. {moved} file{'s' if moved != 1 else ''} restored." if moved else "Rewound."
        )
        if outcome.files.conflicts:
            text += f" Left alone (changed since): {', '.join(outcome.files.conflicts)}"
        self.notify(text, severity="warning" if outcome.files.conflicts else "information")
        if mode in ("both", "conversation"):
            prompt = self.query_one("#prompt", PromptArea)
            prompt.load_text(outcome.prompt)  # edit it and send again
            prompt.focus()
