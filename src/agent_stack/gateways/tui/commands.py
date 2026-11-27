"""The prompt: what Enter does with the text, ``/`` commands and their completion list.

Plain text starts a run, or, while this tab's run is working, is queued into it. A built-in
``/command`` runs here; any other ``/name`` is a skill and goes to the model.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from textual.app import App
from textual.markup import escape
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from agent_stack.gateways.tui.chrome import PromptArea
from agent_stack.gateways.tui.rows import UserMessage

if TYPE_CHECKING:
    from agent_stack.app.application import Application
    from agent_stack.gateways.tui.tabs import Conversation

_QUIT_WORDS = frozenset({"exit", "quit"})

#: ``/name`` or ``/name args``, but not a path such as ``/etc/hosts``.
_COMMAND_RE = re.compile(r"^/[A-Za-z][\w-]*(\s|$)")


class CommandsMixin(App[None]):
    """Part of :class:`~agent_stack.gateways.tui.app.TuiApp`: prompt input and slash commands."""

    if TYPE_CHECKING:  # from the rest of TuiApp
        application: Application
        tab: Conversation
        _principal: str

        def _spawn(self, coro: Any) -> None: ...
        def _busy(self) -> bool: ...
        def _refresh_all(self) -> None: ...
        def _remove_welcome(self, tab: Conversation) -> None: ...
        def _bump_tokens(self, tab: Conversation, text: str) -> None: ...
        async def _start_run(self, tab: Conversation, text: str) -> None: ...
        async def _open_reference(self, reference: str) -> None: ...
        async def action_new_session(self) -> None: ...
        async def action_close_tab(self) -> None: ...
        async def action_switch_session(self) -> None: ...
        async def action_rewind(self) -> None: ...
        async def action_show_branches(self) -> None: ...
        async def action_make_reference(self) -> None: ...
        async def action_restore_files(self) -> None: ...
        def action_help(self) -> None: ...

    def _builtins(self) -> dict[str, tuple[str, Any]]:
        return {
            "new": ("start a new conversation in a new tab", self.action_new_session),
            "close": ("close this tab (the conversation stays stored)", self.action_close_tab),
            "sessions": ("open an earlier conversation", self.action_switch_session),
            "rewind": ("go back to an earlier message", self.action_rewind),
            "branches": ("draw this conversation's branches", self.action_show_branches),
            "ref": ("copy a reference to this point (or /ref <reference> to open one)", None),
            "files": ("put this conversation's files in the folder", self.action_restore_files),
            "mode": ("approval mode: /mode auto or /mode ask", None),
            "model": ("show or switch the model: /model provider:name", None),
            "compact": ("summarise the conversation to free context: /compact [focus]", None),
            "help": ("keyboard shortcuts", self.action_help),
            "quit": ("leave (prints how to continue)", self.action_quit_app),
        }

    def command_entries(self) -> list[tuple[str, str, str]]:
        """Every ``/command`` as ``(name, description, kind)``: built-ins first, then skills."""
        builtins = self._builtins()
        entries = [(name, desc, "builtin") for name, (desc, _) in builtins.items()]
        entries += [
            (c.name, c.description, "skill")
            for c in self.application.slash_commands()
            if c.name not in builtins
        ]
        return entries

    def on_text_area_changed(self, event: Any) -> None:
        """Show the ``/`` completion list while a command name is being typed."""
        popup = self.query_one("#completions", OptionList)
        text = event.text_area.text
        if (
            event.text_area.id != "prompt"
            or not text.startswith("/")
            or any(c.isspace() for c in text)
        ):
            popup.display = False
            return
        query = text[1:].lower()
        matches = [e for e in self.command_entries() if e[0].lower().startswith(query)]
        matches += [e for e in self.command_entries() if query in e[0].lower() and e not in matches]
        if not matches:
            popup.display = False
            return
        popup.clear_options()
        popup.add_options(
            [
                Option(
                    f"[bold $accent]/{name}[/]  [$foreground-muted]{escape(' '.join(desc.split())[:52])}"
                    f"{'  ·skill' if kind == 'skill' else ''}[/]",
                    id=name,
                )
                for name, desc, kind in matches[:30]
            ]
        )
        popup.highlighted = 0
        popup.display = True

    async def action_quit_app(self) -> None:
        self.exit()

    async def _run_slash(self, text: str) -> bool:
        """Handle ``/name args`` if it is a built-in; True when it was (skills go to the model)."""
        head, _, argument = text.strip().partition(" ")
        name, argument = head[1:].lower(), argument.strip()
        if name == "ref":
            if argument.startswith("asref://"):
                await self._open_reference(argument)
            else:
                await self.action_make_reference()
            return True
        if name == "mode":
            await self.action_toggle_approval_mode(argument or None)
            return True
        if name == "model":
            self._switch_model(argument)
            return True
        if name == "compact":
            self._spawn(self._compact(argument))  # waits for a running turn; keep the UI live
            return True
        entry = self._builtins().get(name)
        if entry is None:
            return False
        outcome = entry[1]()
        if outcome is not None:  # most actions are coroutines; help is plain
            await outcome
        return True

    async def _compact(self, focus: str) -> None:
        """``/compact [focus]``: replace the conversation so far with a saved summary."""
        if self.tab.session_id is None:
            self.notify("Nothing to compact yet.")
            return
        self.notify("Compacting the conversation…")
        try:
            count = await self.application.compact_session(self.tab.session_id, focus)
        except Exception as exc:
            self.notify(f"Cannot compact: {exc}", severity="error")
            return
        self.notify(
            f"Compacted {count} messages into a summary; the next message starts from it."
            if count
            else "Nothing new to compact."
        )

    def _switch_model(self, wanted: str) -> None:
        """``/model`` shows the model; ``/model provider:name`` switches it for the next runs."""
        if not wanted:
            self.notify(f"Model: {self.application.get_model()}. Switch: /model provider:name")
            return
        try:
            now = self.application.set_model(wanted)
        except Exception as exc:
            self.notify(f"Cannot switch model: {exc}", severity="error")
            return
        self.notify(f"Model: {now} (from the next message on)")
        self._refresh_all()

    # ── approval mode ─────────────────────────────────────────────────────────
    async def action_toggle_approval_mode(self, wanted: str | None = None) -> None:
        """Flip who answers tool approvals first: you (ask) or the classifier (auto)."""
        current = self.application.get_approval_mode()
        target = wanted or ("auto" if current == "ask" else "ask")
        try:
            now = self.application.set_approval_mode(target)
        except Exception as exc:
            self.notify(f"Cannot switch mode: {exc}", severity="error")
            return
        text = (
            "Auto mode: a classifier decides what is safe; you are asked only when it is unsure."
            if now == "auto"
            else "Ask mode: you approve every gated action."
        )
        self.notify(text, severity="warning" if now == "auto" else "information", timeout=6)
        self._refresh_all()

    # ── input ─────────────────────────────────────────────────────────────────
    async def on_prompt_area_submitted(self, event: PromptArea.Submitted) -> None:
        text = event.text
        if text in _QUIT_WORDS:
            self.exit()
            return
        command = bool(_COMMAND_RE.match(text.strip()))
        if self.tab.running and not command:
            await self._queue_input(text)
            return
        prompt = self.query_one("#prompt", PromptArea)
        if command and text.strip()[1:].split()[0].lower() in self._builtins():
            self.query_one("#completions", OptionList).display = False
            prompt.clear_text()
            await self._run_slash(text)
            return
        if self._busy():
            return
        self.query_one("#completions", OptionList).display = False
        prompt.clear_text()
        if command:
            names = {c.name.lower().replace("_", "-") for c in self.application.slash_commands()}
            if text.strip()[1:].split()[0].lower().replace("_", "-") not in names:
                self.notify(
                    f"Unknown command {text.split()[0]}. Type / to see them.", severity="warning"
                )
                return
        if text.strip().startswith("asref://"):  # a pasted reference opens; it is not a prompt
            await self._open_reference(text.strip())
            return
        await self._submit_prompt(text)

    async def _submit_prompt(self, text: str) -> None:
        tab = self.tab
        self._remove_welcome(tab)
        if tab.session_id is None:
            # A new tab is only a draft: the session is stored on first message, so opening
            # and quitting never leaves an empty conversation behind.
            tab.session_id = await self.application.new_session(self._principal)
        if tab.title is None:
            tab.title = text
            await self._name_session(tab.session_id, text)
        self._bump_tokens(tab, text)
        tab.mount(UserMessage(text))
        delegated = _parse_delegation(text)
        if delegated is not None:
            # Honest delegation: the app can only start a run on the wired agent, so the
            # named subagent is reached by asking the model to delegate to it (P20).
            name, task = delegated
            text = f"Delegate this task to the `{name}` subagent: {task}"
        await self._start_run(tab, text)

    async def _queue_input(self, text: str) -> None:
        """Send a message into the working run; the model reads it before its next step."""
        tab = self.tab
        if not await self.application.send_input(tab.run_id or "", text):
            self.notify("The run had just finished. Press enter to send it.", severity="warning")
            return
        self.query_one("#prompt", PromptArea).clear_text()
        bubble = UserMessage(text, queued=True)
        tab.queued.append(bubble)
        tab.mount(bubble)

    async def action_edit_queued(self) -> None:
        """Take the latest queued message back into the prompt (edit it, or clear it to drop it)."""
        tab = self.tab
        if not tab.queued:
            return
        bubble = tab.queued[-1]
        if not await self.application.withdraw_input(tab.run_id or "", bubble.text):
            self.notify("The agent has already read that message.")
            return
        tab.queued.remove(bubble)
        await bubble.remove()
        prompt = self.query_one("#prompt", PromptArea)
        prompt.load_text(bubble.text)
        prompt.move_cursor(prompt.document.end)

    async def _name_session(self, session_id: str, text: str) -> None:
        """Persist the first message as the session's title so it is findable later."""
        try:
            await self.application.set_session_title(session_id, text[:80])
        except Exception:  # a cosmetic title must never block the message
            return


def _parse_delegation(text: str) -> tuple[str, str] | None:
    """Parse ``/agent <name> <task>`` or ``@<name> <task>`` into ``(name, task)``."""
    if text.startswith("/agent "):
        rest = text[len("/agent ") :].strip()
    elif text.startswith("@"):
        rest = text[1:].strip()
    else:
        return None
    parts = rest.split(maxsplit=1)
    if len(parts) < 2:
        return None
    return parts[0], parts[1]
