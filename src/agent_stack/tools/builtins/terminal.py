"""Run commands as sandboxed subprocesses, never through an implicit shell.

The runner is deliberately explicit: it uses ``create_subprocess_exec``, starts each command
in its own process group so the whole tree can be terminated on timeout or cancellation,
passes a filtered environment, and caps stdout/stderr. The result is structured so callers
(and the agent) can reason about exit codes and timeouts without parsing output.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import os
import re
import signal
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from agent_stack.core.context import TRUNCATION_MARKER
from agent_stack.core.errors import CommandTimeoutError
from agent_stack.core.tool_stream import ToolOutputSink, current_tool_output_sink
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

#: The only environment variables a child process may inherit.
ENV_ALLOWLIST = ("PATH", "HOME", "LANG")

DEFAULT_TIMEOUT_SECONDS = 30.0

#: How long a process group gets to exit on ``SIGTERM`` before ``SIGKILL``.
_KILL_GRACE_SECONDS = 1.0

#: Bytes read from a pipe per iteration while streaming.
_READ_CHUNK_BYTES = 4096

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
    keep = max(0, limit - 1)  # reserve one byte for the marker's leading newline
    return data[:keep].decode("utf-8", errors="replace") + "\n" + TRUNCATION_MARKER


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


async def _pump(
    reader: asyncio.StreamReader,
    stream: str,
    max_bytes: int,
    on_output: ToolOutputSink | None,
) -> bytes:
    """Read one pipe to EOF, returning every byte read.

    Every chunk is also handed to ``on_output`` as decoded text — but only until
    ``max_bytes`` have been emitted, so live events stay bounded. A single incremental
    UTF-8 decoder spans the reads so a character split across chunks is not corrupted.
    """
    collected = bytearray()
    emitted = 0
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    while True:
        chunk = await reader.read(_READ_CHUNK_BYTES)
        if not chunk:
            break
        collected.extend(chunk)
        if on_output is not None and emitted < max_bytes:
            emitted += len(chunk)
            text = decoder.decode(chunk)
            if text:
                await on_output(stream, text)
    if on_output is not None and emitted < max_bytes:
        tail = decoder.decode(b"", final=True)
        if tail:
            await on_output(stream, tail)
    return bytes(collected)


async def run_command(
    command: str | Sequence[str],
    *,
    cwd: Path,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    shell: bool = False,
    max_stdout_bytes: int = MAX_STREAM_BYTES,
    max_stderr_bytes: int = MAX_STREAM_BYTES,
    raise_on_timeout: bool = False,
    on_output: ToolOutputSink | None = None,
) -> CommandResult:
    """Run ``command`` in ``cwd`` and return a structured :class:`CommandResult`.

    ``shell`` must be set explicitly to run through ``/bin/sh -c``; by default the argument
    vector is executed directly. On timeout the whole process group is killed and
    ``timed_out=True`` is reported — unless ``raise_on_timeout`` is set, in which case a
    :class:`~agent_stack.core.errors.CommandTimeoutError` is raised after cleanup.

    When ``on_output`` is given, each stream is read in chunks and every chunk is passed to
    it as ``(stream, text)`` while the command runs (bounded per stream by the byte caps),
    so callers can render live output.
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

    async def read_streams() -> tuple[bytes, bytes]:
        # Both pipes are set to PIPE above, so the readers exist.
        stdout = process.stdout
        stderr = process.stderr
        assert stdout is not None and stderr is not None
        return await asyncio.gather(
            _pump(stdout, "stdout", max_stdout_bytes, on_output),
            _pump(stderr, "stderr", max_stderr_bytes, on_output),
        )

    timed_out = False
    read_task: asyncio.Task[tuple[bytes, bytes]] = asyncio.ensure_future(read_streams())
    try:
        done, _ = await asyncio.wait({read_task}, timeout=timeout)
    except asyncio.CancelledError:
        await _kill_process_group(process)
        await asyncio.gather(read_task, return_exceptions=True)
        raise

    if read_task in done:
        stdout_bytes, stderr_bytes = read_task.result()
    else:
        timed_out = True
        await _kill_process_group(process)
        stdout_bytes, stderr_bytes = await read_task

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


#: Absolute paths a command may always mention: system binaries/libraries and scratch space.
_SAFE_PREFIXES = (
    "/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/dev", "/proc", "/sys",
    "/opt", "/tmp", "/var/tmp", "/private/tmp", "/private/var/folders", "/var/folders",
    "/System", "/Library", "/Applications", "/nix",
)  # fmt: skip
_PATH_TOKEN_RE = re.compile(r"""(?<![\w.$:/-])(~[\w-]*/?[^\s'";|&<>()`]*|/[^\s'";|&<>()`]*)""")


def outside_paths(command: str, root: Path) -> list[str]:
    """Absolute or home paths in ``command`` that lie outside ``root`` and the safe system dirs.

    A guard against accidents (``find /``, editing a sibling repo), not a security boundary:
    a determined command can hide a path in a variable or a script. Use a container for that.
    """
    found: list[str] = []
    for token in _PATH_TOKEN_RE.findall(command):
        path = Path(token).expanduser()
        if token in ("/",) or str(path) == "/":
            found.append(token)
            continue
        text = str(path)
        if path.is_relative_to(root) or any(
            text == p or text.startswith(p + "/") for p in _SAFE_PREFIXES
        ):
            continue
        found.append(token)
    return found


def run_command_tool(policy: WorkspacePolicy, *, confine: bool = True) -> BaseTool:
    """Build the ``run_command`` tool, defaulting ``cwd`` to the workspace root."""

    @tool("run_command")
    async def _run_command_tool(
        command: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        shell: bool = False,
        cwd: str = ".",
    ) -> str:
        """Run a command in the workspace and capture stdout/stderr.

        No implicit shell: the command is executed directly unless ``shell=True`` is
        passed explicitly. Prefer the dedicated tools for filesystem work — ``read_file``
        (not cat/head/tail), ``edit_file`` (not sed/awk), ``write_file`` (not echo),
        ``search_files``/``grep_files``/``list_directory`` (not find/grep/ls); they are scoped, capped, and
        easier to review. The working directory resets each call to the workspace root
        unless ``cwd`` is given. Output is capped and a non-zero exit code is reported in
        the result, not raised; the whole process group is killed if the command exceeds
        ``timeout`` (reported as timed out).
        """
        working_dir = policy.resolve(cwd)
        if confine and not policy.allow_external:
            outside = outside_paths(command, policy.root)
            if outside:
                return (
                    f"Error: this command touches paths outside the workspace "
                    f"({', '.join(outside[:3])}); the workspace is {policy.root}. "
                    "Work inside it, or ask the user to run this themselves."
                )
        result = await run_command(
            command,
            cwd=working_dir,
            timeout=timeout,
            shell=shell,
            on_output=current_tool_output_sink(),
        )
        return result.render()

    return _run_command_tool
