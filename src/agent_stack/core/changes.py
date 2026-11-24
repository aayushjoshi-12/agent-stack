"""Files the agent created or edited: the record, the recorder protocol and the diff.

The filesystem tools report each ``write_file``/``edit_file`` here with the file's text before
and after, so a gateway can show *what actually changed* (the event log caps every field at 2 KB
and so cannot). Only changes made through those two tools are known: a file changed by a shell
command (``run_command``) is not tracked.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Literal, Protocol

__all__ = [
    "ChangeEntry",
    "ChangeRecord",
    "ChangeRecorder",
    "ChangeStore",
    "FileDiff",
    "make_diff",
]

#: A diff longer than this many lines is cut (a generated file can be thousands of lines).
MAX_DIFF_LINES = 4000

ChangeKind = Literal["created", "modified"]


@dataclass(frozen=True)
class ChangeRecord:
    """One write or edit of one file, with where and when it happened."""

    id: int
    session_id: str
    run_id: str
    path: str  # relative to the workspace root, POSIX style
    created_at: str


@dataclass(frozen=True)
class ChangeEntry:
    """One recorded edit with its full before/after text (what rewinding needs)."""

    id: int
    session_id: str
    run_id: str
    path: str
    before: str | None
    after: str
    created_at: str


@dataclass(frozen=True)
class FileDiff:
    """The net change to one file across the edits being looked at."""

    path: str
    kind: ChangeKind  # created: it did not exist before the first edit
    edits: int
    added: int
    removed: int
    diff: str  # unified diff text, ``""`` when the net change is empty
    before: str | None  # text before the first edit; ``None`` for a created file
    after: str  # text after the last edit
    diff_truncated: bool = False


class ChangeRecorder(Protocol):
    """What the filesystem tools call after a successful write or edit."""

    async def record(
        self, *, session_id: str, run_id: str, path: str, before: str | None, after: str
    ) -> None: ...


class ChangeStore(ChangeRecorder, Protocol):
    """Recorder plus the reads a gateway needs."""

    async def paths(self, session_id: str, run_id: str | None = None) -> list[str]:
        """Distinct changed paths of a conversation (subagent runs included), oldest first."""
        ...

    async def file_diff(
        self, session_id: str, path: str, run_id: str | None = None, *, last_only: bool = False
    ) -> FileDiff | None:
        """The net diff of ``path`` over the conversation (or one run), or ``None``.

        ``last_only`` diffs just the most recent edit (what one tool call did).
        """
        ...

    async def delete_session(self, session_id: str) -> None: ...

    async def entries(self, session_id: str) -> list[ChangeEntry]:
        """Every recorded edit of a conversation (subagent runs included), oldest first."""
        ...

    async def folder_state(self) -> str | None:
        """The conversation whose file state the working folder was last put in, if known."""
        ...

    async def set_folder_state(self, session_id: str) -> None: ...

    async def copy_upto(self, source: str, target: str, upto: str) -> int:
        """Copy ``source``'s edits made at or before ``upto`` (ISO time) into ``target``.

        Snapshots are copied too, so deleting ``source`` later cannot break ``target``.
        Returns how many edits were copied.
        """
        ...


def make_diff(path: str, before: str | None, after: str) -> tuple[str, int, int, bool]:
    """A unified diff of ``before`` -> ``after`` as ``(text, added, removed, truncated)``.

    ``before=None`` means the file did not exist (shown as additions against ``/dev/null``).
    """
    old = [] if before is None else before.splitlines()
    new = after.splitlines()
    lines = list(
        difflib.unified_diff(
            old,
            new,
            fromfile="/dev/null" if before is None else f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
            n=3,
        )
    )
    added = sum(1 for ln in lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in lines if ln.startswith("-") and not ln.startswith("---"))
    truncated = len(lines) > MAX_DIFF_LINES
    if truncated:
        lines = lines[:MAX_DIFF_LINES]
    return "\n".join(lines), added, removed, truncated
