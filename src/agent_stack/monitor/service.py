"""A poller that watches a shell command and queues a note only when something happens.

The point is to spend no model calls while nothing changes. A watch runs a command every
``interval`` seconds and says something only when:

* ``change``: the output differs from the previous poll (the first poll is the baseline);
* ``match``: the output contains a regex (each distinct matching output once);
* ``exit``: the command succeeds (exit code 0), e.g. ``test -f build/done``; the watch then ends.

A watch also ends on its own after ``max_seconds`` or ``max_notes`` notes, so a forgotten watch
cannot run or chatter forever. Notes wait in a per-session queue; ``Application.send_message``
puts them in front of the next message, so the agent sees them without a poll of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from agent_stack.tools.builtins.terminal import CommandResult, run_command

__all__ = ["MODES", "MonitorError", "MonitorService", "Watch"]

_LOG = logging.getLogger(__name__)
MODES = ("change", "match", "exit")
MIN_INTERVAL = 5.0
MAX_WATCHES_PER_SESSION = 5
_NOTE_CHARS = 1_500
_MAX_PENDING = 50

Runner = Callable[[str, Path], Awaitable[CommandResult]]


class MonitorError(ValueError):
    """A request the monitor refuses (bad mode, interval, pattern or too many watches)."""


@dataclass
class Watch:
    id: str
    session_id: str
    command: str
    mode: str
    pattern: str | None
    interval: float
    max_seconds: float
    max_notes: int
    started: float = field(default_factory=time.monotonic)
    polls: int = 0
    notes: int = 0
    status: str = "running"  # running | done | expired | stopped | failed
    last_output: str | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def describe(self) -> str:
        what = {"change": "any change", "match": f"matches /{self.pattern}/", "exit": "success"}
        age = int(time.monotonic() - self.started)
        return (
            f"{self.id} [{self.status}] every {self.interval:g}s until {what[self.mode]}: "
            f"{self.command}  (polls {self.polls}, notes {self.notes}, {age}s old)"
        )


async def _shell(command: str, cwd: Path) -> CommandResult:
    return await run_command(command, cwd=cwd, shell=True, timeout=60.0)


class MonitorService:
    """Owns the watches. One per application; watches belong to a session."""

    def __init__(
        self,
        workspace: Path,
        *,
        runner: Runner = _shell,
        min_interval: float = MIN_INTERVAL,
        on_note: Callable[[str, str], None] | None = None,
    ) -> None:
        self._workspace = workspace
        self._runner = runner
        self._min_interval = min_interval
        self._on_note = on_note
        self._watches: dict[str, Watch] = {}
        self._pending: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=_MAX_PENDING))

    # ── control ──────────────────────────────────────────────────────────────
    def start(
        self,
        session_id: str,
        command: str,
        *,
        mode: str = "change",
        pattern: str | None = None,
        interval: float = 30.0,
        max_minutes: float = 60.0,
        max_notes: int = 10,
    ) -> Watch:
        if mode not in MODES:
            raise MonitorError(f"mode must be one of {', '.join(MODES)}")
        if not command.strip():
            raise MonitorError("command must not be empty")
        if mode == "match":
            if not pattern:
                raise MonitorError("mode 'match' needs a pattern")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise MonitorError(f"bad pattern: {exc}") from exc
        if interval < self._min_interval:
            raise MonitorError(f"interval must be at least {self._min_interval:g} seconds")
        running = [w for w in self.list(session_id) if w.status == "running"]
        if len(running) >= MAX_WATCHES_PER_SESSION:
            raise MonitorError(f"at most {MAX_WATCHES_PER_SESSION} running monitors per session")
        watch = Watch(
            id=f"mon-{uuid4().hex[:6]}",
            session_id=session_id,
            command=command,
            mode=mode,
            pattern=pattern,
            interval=interval,
            max_seconds=max(0.001, max_minutes * 60.0),
            max_notes=max(1, max_notes),
        )
        self._watches[watch.id] = watch
        watch.task = asyncio.ensure_future(self._poll(watch))
        return watch

    def stop(self, session_id: str, watch_id: str) -> bool:
        watch = self._watches.get(watch_id)
        if watch is None or watch.session_id != session_id:
            return False
        if watch.status == "running":
            watch.status = "stopped"
            if watch.task is not None:
                watch.task.cancel()
        return True

    def list(self, session_id: str) -> list[Watch]:
        return [w for w in self._watches.values() if w.session_id == session_id]

    async def stop_all(self) -> None:
        tasks = []
        for watch in self._watches.values():
            if watch.task is not None and not watch.task.done():
                watch.status = "stopped" if watch.status == "running" else watch.status
                watch.task.cancel()
                tasks.append(watch.task)
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── notes ────────────────────────────────────────────────────────────────
    def drain(self, session_id: str) -> str | None:
        """Take the session's waiting notes as one block, or ``None`` when there are none."""
        queue = self._pending.get(session_id)
        if not queue:
            return None
        lines = list(queue)
        queue.clear()
        return "[Monitor notes since your last turn]\n" + "\n\n".join(lines)

    def peek(self, session_id: str) -> int:
        return len(self._pending.get(session_id, ()))

    def _note(self, watch: Watch, text: str) -> None:
        watch.notes += 1
        body = text.strip()
        if len(body) > _NOTE_CHARS:
            body = body[:_NOTE_CHARS] + "\n…[truncated]"
        self._pending[watch.session_id].append(f"{watch.id} ({watch.command}):\n{body}")
        if self._on_note is not None:
            with contextlib.suppress(Exception):  # a gateway hook must never kill a watch
                self._on_note(watch.session_id, watch.id)

    # ── the loop ─────────────────────────────────────────────────────────────
    async def _poll(self, watch: Watch) -> None:
        try:
            while True:
                if time.monotonic() - watch.started >= watch.max_seconds:
                    watch.status = "expired"
                    self._note(watch, f"stopped: it ran for {int(watch.max_seconds // 60)} minutes")
                    return
                try:
                    result = await self._runner(watch.command, self._workspace)
                except Exception as exc:  # the command could not even start
                    watch.status = "failed"
                    self._note(watch, f"stopped: could not run the command ({exc})")
                    return
                watch.polls += 1
                if self._decide(watch, result):
                    if watch.mode == "exit":
                        watch.status = "done"
                        return
                    if watch.notes >= watch.max_notes:
                        watch.status = "done"
                        self._note(watch, "stopped: it reached its limit of notes")
                        return
                await asyncio.sleep(watch.interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - defensive
            _LOG.exception("monitor %s crashed", watch.id)
            watch.status = "failed"

    def _decide(self, watch: Watch, result: CommandResult) -> bool:
        """Queue a note when this poll is worth one; True when it did."""
        output = (result.stdout + result.stderr).strip()
        if result.timed_out:
            output = "(the command timed out)\n" + output
        previous, watch.last_output = watch.last_output, output
        if watch.mode == "exit":
            if result.exit_code == 0 and not result.timed_out:
                self._note(watch, f"succeeded (exit 0):\n{output}")
                return True
            return False
        if watch.mode == "match":
            assert watch.pattern is not None
            if re.search(watch.pattern, output) and output != previous:
                self._note(watch, f"matched /{watch.pattern}/:\n{output}")
                return True
            return False
        if previous is not None and output != previous:
            self._note(watch, f"output changed.\nbefore:\n{previous}\n\nafter:\n{output}")
            return True
        return False
