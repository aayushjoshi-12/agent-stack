"""Run commands as sandboxed subprocesses, never through an implicit shell.

The runner is deliberately explicit: it uses ``create_subprocess_exec``, starts each command
in its own process group so the whole tree can be terminated on timeout or cancellation,
passes a filtered environment, and caps stdout/stderr. The result is structured so callers
(and the agent) can reason about exit codes and timeouts without parsing output.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from agent_stack.core.errors import CommandTimeoutError
from agent_stack.core.workspace import WorkspacePolicy

__all__ = [
    "MAX_STREAM_BYTES",
    "TRUNCATION_MARKER",
    "CommandResult",
    "run_command",
    "run_command_tool",
]

#: Largest amount of stdout/stderr captured per stream.
MAX_STREAM_BYTES = 32 * 1024

#: Appended to a stream that was cut off at the cap.
TRUNCATION_MARKER = "\n…[truncated]"

#: The only environment variables a child process may inherit.
ENV_ALLOWLIST = ("PATH", "HOME", "LANG")

DEFAULT_TIMEOUT_SECONDS = 30.0

#: How long a process group gets to exit on ``SIGTERM`` before ``SIGKILL``.
_KILL_GRACE_SECONDS = 1.0

_SHELL = "/bin/sh"


@dataclass(frozen=True)
class CommandResult:
    """The structured outcome of a finished (or killed) command."""

    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: float
    timed_out: bool

    @property
    def ok(self) -> bool:
        """True only when the command finished on its own with exit code 0."""
        return not self.timed_out and self.exit_code == 0

    def render(self) -> str:
        """A concise, model-facing summary of the result."""
        status = "timed out" if self.timed_out else f"exited with code {self.exit_code}"
        parts = [f"Command {status} in {self.duration_ms:.0f} ms."]
        if self.stdout:
            parts.append(f"stdout:\n{self.stdout}")
        if self.stderr:
            parts.append(f"stderr:\n{self.stderr}")
        return "\n".join(parts)


def _filtered_env() -> dict[str, str]:
    return {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}


def _build_argv(command: str | Sequence[str], shell: bool) -> list[str]:
    if shell:
        script = command if isinstance(command, str) else " ".join(command)
        return [_SHELL, "-c", script]
    return [command] if isinstance(command, str) else list(command)


def _truncate(data: bytes, limit: int) -> str:
    if len(data) <= limit:
        return data.decode("utf-8", errors="replace")
    return data[:limit].decode("utf-8", errors="replace") + TRUNCATION_MARKER


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    # The process (group) may already have exited — nothing left to signal.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


async def _kill_process_group(process: asyncio.subprocess.Process) -> None:
    """Terminate the process group gently, then forcefully if it lingers."""
    if process.returncode is not None:
        return

    try:
        pgid = os.getpgid(process.pid)
    except ProcessLookupError:
        return

    _signal_group(pgid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=_KILL_GRACE_SECONDS)
    except TimeoutError:
        _signal_group(pgid, signal.SIGKILL)
        await process.wait()


async def run_command(
    command: str | Sequence[str],
    *,
    cwd: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    shell: bool = False,
    max_stdout_bytes: int = MAX_STREAM_BYTES,
    max_stderr_bytes: int = MAX_STREAM_BYTES,
    raise_on_timeout: bool = False,
) -> CommandResult:
    """Run ``command`` in ``cwd`` and return a structured :class:`CommandResult`.

    ``shell`` must be set explicitly to run through ``/bin/sh -c``; by default the argument
    vector is executed directly. On timeout the whole process group is killed and
    ``timed_out=True`` is reported — unless ``raise_on_timeout`` is set, in which case a
    :class:`~agent_stack.core.errors.CommandTimeoutError` is raised after cleanup.
    """
    argv = _build_argv(command, shell)
    if not argv:
        raise ValueError("command must not be empty")

    start = time.perf_counter()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=_filtered_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    timed_out = False
    communicate: asyncio.Task[tuple[bytes, bytes]] = asyncio.ensure_future(process.communicate())
    try:
        done, _ = await asyncio.wait({communicate}, timeout=timeout)
    except asyncio.CancelledError:
        await _kill_process_group(process)
        await asyncio.gather(communicate, return_exceptions=True)
        raise

    if communicate in done:
        stdout_bytes, stderr_bytes = communicate.result()
    else:
        timed_out = True
        await _kill_process_group(process)
        stdout_bytes, stderr_bytes = await communicate

    duration_ms = (time.perf_counter() - start) * 1000.0
    result = CommandResult(
        exit_code=process.returncode,
        stdout=_truncate(stdout_bytes, max_stdout_bytes),
        stderr=_truncate(stderr_bytes, max_stderr_bytes),
        duration_ms=duration_ms,
        timed_out=timed_out,
    )

    if timed_out and raise_on_timeout:
        raise CommandTimeoutError(f"Command {argv!r} timed out after {timeout:g}s.")

    return result


def run_command_tool(policy: WorkspacePolicy) -> BaseTool:
    """Build the ``run_command`` tool, defaulting ``cwd`` to the workspace root."""

    @tool("run_command")
    async def _run_command_tool(
        command: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        shell: bool = False,
        cwd: str = ".",
    ) -> str:
        """Run a command in the workspace and capture its output.

        ``command`` runs directly, without a shell, unless ``shell=True`` is passed
        explicitly. ``cwd`` is resolved relative to the workspace root. Output is capped
        and a non-zero exit code is reported in the result rather than raised.
        """
        working_dir = policy.resolve(cwd)
        result = await run_command(command, cwd=working_dir, timeout=timeout, shell=shell)
        return result.render()

    return _run_command_tool
