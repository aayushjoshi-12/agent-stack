"""Modal screens: approval, the conversation picker, rewind, changed files and branches.

Each one shows what the app hands it and answers through ``dismiss`` (or, for the picker's
delete, a callback the app supplies); none touches storage or the runtime itself.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, ClassVar

from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from agent_stack.app.application import FileDiff
from agent_stack.gateways.base import SessionSummary, format_when
from agent_stack.gateways.tui.rows import diff_rows

if TYPE_CHECKING:
    from agent_stack.app.application import LineTurn

__all__ = [
    "ApprovalScreen",
    "BranchesScreen",
    "ChangesScreen",
    "RewindScreen",
    "SessionPicker",
]


class ChangesScreen(ModalScreen[None]):
    """The files the agent created or edited: pick one to read its diff.

    Shows the *net* change per file (first state to last), as the web UI does. Only
    ``write_file``/``edit_file`` are tracked, which the footer says.
    """

    BINDINGS: ClassVar[list[Any]] = [Binding("escape,q,ctrl+g", "close", "close")]
    FOOT = (
        "[$foreground-muted]↑↓ choose · esc close · only files the agent wrote or edited "
        "with its file tools are listed, not files changed by shell commands[/]"
    )

    def __init__(self, files: list[FileDiff]) -> None:
        super().__init__()
        self._files = files

    def compose(self):  # type: ignore[no-untyped-def]
        with Vertical(id="changes-card"):
            count = len(self._files)
            yield Static(
                f"[bold $primary]Files changed[/]  [$foreground-muted]{count} "
                f"file{'' if count == 1 else 's'}[/]",
                id="changes-title",
            )
            if not self._files:
                yield Static("[$foreground-muted]The agent has not changed any files yet.[/]")
            else:
                yield OptionList(*(self._option(f) for f in self._files), id="changes-list")
                with VerticalScroll(id="changes-scroll"):
                    yield Static("", id="changes-diff")
            yield Static(self.FOOT, id="changes-foot")

    @staticmethod
    def _option(file: FileDiff) -> Option:
        kind = "[$success]new[/]" if file.kind == "created" else "[$accent]edit[/]"
        edits = f" x{file.edits}" if file.edits > 1 else ""
        stats = f"[$success]+{file.added}[/] [$error]-{file.removed}[/]"
        return Option(
            f"{kind} {escape(file.path)}  {stats}[$foreground-muted]{edits}[/]", id=file.path
        )

    def on_mount(self) -> None:
        if self._files:
            listing = self.query_one(OptionList)
            listing.focus()
            listing.highlighted = 0
            self._show(self._files[0])

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        for file in self._files:
            if file.path == event.option.id:
                self._show(file)
                return

    def _show(self, file: FileDiff) -> None:
        if not file.diff:
            body = "[$foreground-muted]No net difference: the edits cancelled each other out.[/]"
        else:
            width = max(0, self.query_one("#changes-scroll").size.width - 2)
            body = "\n".join(diff_rows(file.diff.splitlines(), width))
            if file.diff_truncated:
                body += "\n[$foreground-muted italic]… diff cut at 4000 lines[/]"
        self.query_one("#changes-diff", Static).update(body)
        self.query_one("#changes-scroll", VerticalScroll).scroll_home(animate=False)

    def action_close(self) -> None:
        self.dismiss(None)


class SessionPicker(ModalScreen[str | None]):
    """Pick an earlier conversation: Enter opens it, ``d`` (twice) deletes it, Esc closes.

    ``delete`` is supplied by the app, so the picker never touches storage itself.
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("escape,q", "close", "close"),
        Binding("d,delete", "delete", "delete"),
    ]
    FOOT = "[$foreground-muted]enter open · d delete · esc close[/]"

    def __init__(
        self,
        sessions: list[SessionSummary],
        current: str | None,
        delete: Callable[[str], Awaitable[None]],
    ) -> None:
        super().__init__()
        self._sessions = sessions
        self._current = current
        self._delete = delete
        self._pending: str | None = None  # the id awaiting a second `d`

    def compose(self):  # type: ignore[no-untyped-def]
        with Vertical(id="picker-card"):
            yield Static("[bold $primary]Conversations[/]", id="picker-title")
            yield OptionList(*(self._option(s) for s in self._sessions), id="picker-list")
            yield Static(self.FOOT if self._sessions else self._EMPTY, id="picker-foot")

    _EMPTY = "[$foreground-muted]No conversations yet. esc to close[/]"

    def _option(self, session: SessionSummary) -> Option:
        """Two lines and a gap: the title on one line, its date and kind beneath, muted."""
        here = session.id == self._current
        mark = "[$accent]●[/] " if here else "  "
        tag = "  [$accent](open now)[/]" if here else ""
        title = " ".join(session.title.split())
        shown = title if len(title) <= 60 else title[:59] + "…"
        facts = [format_when(session.updated_at or session.created_at)]
        if session.branch_of:
            facts.append("a branch")
        meta = "  ·  ".join(f for f in facts if f)
        lines = [
            f"{mark}[bold]{escape(shown)}[/]{tag}",
            f"  [$foreground-muted]{escape(meta)}[/]",
            "",
        ]
        return Option("\n".join(lines), id=session.id)

    def on_mount(self) -> None:
        listing = self.query_one(OptionList)
        listing.focus()
        for index, session in enumerate(self._sessions):
            if session.id == self._current:
                listing.highlighted = index

    def _foot(self, markup: str) -> None:
        self.query_one("#picker-foot", Static).update(markup)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if self._pending is not None and event.option.id != self._pending:
            self._pending = None
            self._foot(self.FOOT)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    async def action_delete(self) -> None:
        listing = self.query_one(OptionList)
        if listing.highlighted is None or listing.option_count == 0:
            return
        session_id = listing.get_option_at_index(listing.highlighted).id
        if session_id is None:
            return
        if self._pending != session_id:  # first press asks, second press deletes
            self._pending = session_id
            self._foot("[$warning]Press d again to delete this conversation for good[/]")
            return
        self._pending = None
        try:
            await self._delete(session_id)
        except Exception as exc:  # a failed delete must not take the picker (or app) down
            self.notify(f"Could not delete: {exc}", severity="error")
            return
        listing.remove_option(session_id)
        self._foot(self.FOOT if listing.option_count else self._EMPTY)

    def action_close(self) -> None:
        self.dismiss(None)


class BranchesScreen(ModalScreen[None]):
    """The branch tree of the open conversation as text; Esc closes."""

    BINDINGS: ClassVar[list[Any]] = [Binding("escape,q", "close", "close")]

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self):  # type: ignore[no-untyped-def]
        with Vertical(id="picker-card", classes="wide"):
            yield Static(
                "[bold $primary]Branches[/]  [$foreground-muted]* is this one[/]", id="picker-title"
            )
            with VerticalScroll(id="changes-scroll"):
                yield Static(escape(self._text))
            yield Static("[$foreground-muted]esc close[/]", id="picker-foot")

    def action_close(self) -> None:
        self.dismiss(None)


#: What a rewind restores, in the order the picker offers them.
REWIND_CHOICES = (
    ("both", "Restore code and conversation"),
    ("conversation", "Restore conversation (keep the code as it is now)"),
    ("code", "Restore code (keep the conversation)"),
    ("cancel", "Never mind"),
)


class RewindScreen(ModalScreen["tuple[LineTurn, str] | None"]):
    """Go back to an earlier message, like Claude Code's double-Esc.

    Step one picks the message (newest highlighted, since that is usually the one you want),
    step two what to restore. Nothing is deleted: the conversation you leave stays in the chats
    list, so a rewind can always be undone by opening it again.
    """

    BINDINGS: ClassVar[list[Any]] = [Binding("escape,q", "back", "back")]
    FOOT_TURNS = "[$foreground-muted]enter choose · esc close[/]"
    FOOT_MODE = "[$foreground-muted]enter restore · esc back[/]"

    def __init__(self, turns: list[LineTurn]) -> None:
        super().__init__()
        self._turns = turns
        self._picked: LineTurn | None = None

    def compose(self):  # type: ignore[no-untyped-def]
        with Vertical(id="picker-card"):
            yield Static(
                "[bold $primary]Rewind[/]  [$foreground-muted]go back to…[/]", id="picker-title"
            )
            yield OptionList(*self._turn_options(), id="picker-list")
            yield Static(self.FOOT_TURNS, id="picker-foot")

    def _turn_options(self) -> list[Option]:
        rows = []
        for number, item in enumerate(self._turns):
            files = item.turn.files_changed
            tail = (
                f"  [$foreground-muted]{files} file{'s' if files != 1 else ''}[/]" if files else ""
            )
            text = escape(" ".join(item.turn.user_text.split())[:60] or "(no text)")
            when = format_when(item.turn.timestamp.isoformat())
            rows.append(
                Option(
                    f"[bold]{text}[/]\n  [$foreground-muted]{escape(when)}[/]{tail}\n",
                    id=str(number),
                )
            )
        return rows

    def on_mount(self) -> None:
        listing = self.query_one(OptionList)
        listing.focus()
        listing.highlighted = max(0, len(self._turns) - 1)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        listing = self.query_one(OptionList)
        if self._picked is None:
            self._picked = self._turns[int(event.option.id or 0)]
            listing.clear_options()
            listing.add_options([Option(label, id=key) for key, label in REWIND_CHOICES])
            listing.highlighted = 0
            title = escape(" ".join(self._picked.turn.user_text.split())[:50])
            self.query_one("#picker-title", Static).update(
                f"[bold $primary]Rewind to before[/] {title}"
            )
            self.query_one("#picker-foot", Static).update(self.FOOT_MODE)
            return
        if event.option.id == "cancel":
            self.dismiss(None)
        else:
            self.dismiss((self._picked, str(event.option.id)))

    def action_back(self) -> None:
        if self._picked is None:
            self.dismiss(None)
            return
        self._picked = None
        listing = self.query_one(OptionList)
        listing.clear_options()
        listing.add_options(self._turn_options())
        listing.highlighted = max(0, len(self._turns) - 1)
        self.query_one("#picker-title", Static).update(
            "[bold $primary]Rewind[/]  [$foreground-muted]go back to…[/]"
        )
        self.query_one("#picker-foot", Static).update(self.FOOT_TURNS)


class ApprovalScreen(ModalScreen[bool]):
    """Ask whether a gated tool call may run: ``y`` approves, ``n`` / Esc rejects.

    There is deliberately no Enter shortcut, so a stray keypress cannot approve a command.
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("y", "answer(True)", "approve"),
        Binding("n,escape", "answer(False)", "reject"),
    ]

    def __init__(self, tool: str, detail: str, note: str = "") -> None:
        super().__init__()
        self._tool = tool
        self._detail = detail
        self._note = note  # why auto mode left this one to you

    def compose(self):  # type: ignore[no-untyped-def]
        with Vertical(id="approval-card"):
            yield Static("[bold $warning]Approval needed[/]", id="approval-title")
            yield Static(
                f"The agent wants to run [bold $accent]{escape(self._tool)}[/]:",
                id="approval-what",
            )
            yield Static(escape(self._detail) or "(no arguments)", id="approval-detail")
            if self._note:
                yield Static(
                    f"[$foreground-muted]Auto mode left this to you:[/] {escape(self._note)}",
                    id="approval-note",
                )
            yield Static(
                "[bold $success]y[/] approve    [bold $error]n[/] / esc reject",
                id="approval-foot",
            )

    def action_answer(self, approved: bool) -> None:
        self.dismiss(approved)
