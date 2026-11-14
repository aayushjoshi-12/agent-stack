"""Filesystem tools — read-only inspection and controlled mutation inside a workspace.

Every tool is a real :class:`~langchain_core.tools.BaseTool` that closes over a
:class:`~agent_stack.core.workspace.WorkspacePolicy`. Paths are always resolved through the
policy, so escapes raise :class:`~agent_stack.core.errors.WorkspaceEscapeError` instead of
touching files outside the workspace. Reads and writes are capped so a tool can never blow
up the model's context by accident.
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import time
from pathlib import Path

from langchain_core.tools import BaseTool, tool

from agent_stack.core.errors import ToolError
from agent_stack.core.workspace import WorkspacePolicy

__all__ = ["MAX_FILE_BYTES", "MAX_GREP_SECONDS", "MAX_SEARCH_RESULTS", "filesystem_tools"]

#: Largest single file read into or written out of context.
MAX_FILE_BYTES = 1024 * 1024  # 1 MiB

#: Most matches ``search_files`` will return before stopping.
MAX_SEARCH_RESULTS = 200

#: Wall-clock budget for one ``grep_files`` call; a slow pattern on a big tree stops here.
MAX_GREP_SECONDS = 10.0

#: Longest line text a grep match shows (minified files have megabyte lines).
_MAX_LINE_CHARS = 200

#: Directories grep never enters: VCS data, dependencies, caches and the agent's own state.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".agent",
    }
)


def _render_size(num_bytes: int) -> str:
    return f"{num_bytes} bytes"


def _list_directory(policy: WorkspacePolicy, path: str) -> str:
    target = policy.resolve(path)
    if not target.exists():
        raise ToolError(f"No such directory: {path}")
    if not target.is_dir():
        raise ToolError(f"Not a directory: {path}")

    entries = sorted(target.iterdir(), key=lambda entry: entry.name)
    if not entries:
        return "(empty directory)"

    lines: list[str] = []
    for entry in entries:
        name = policy.relative(entry)
        if entry.is_dir():
            lines.append(f"{name}/")
        else:
            lines.append(f"{name} ({_render_size(entry.stat().st_size)})")
    return "\n".join(lines)


def _read_file(policy: WorkspacePolicy, path: str) -> str:
    target = policy.resolve(path)
    if not target.is_file():
        raise ToolError(f"Not a file: {path}")

    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ToolError(
            f"File too large to read ({_render_size(size)} > {_render_size(MAX_FILE_BYTES)}): "
            f"{path}. Read a smaller slice or search instead."
        )

    return target.read_text(encoding="utf-8", errors="replace")


def _write_file(policy: WorkspacePolicy, path: str, content: str) -> str:
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise ToolError(
            f"Content too large to write ({_render_size(len(data))} > "
            f"{_render_size(MAX_FILE_BYTES)}): {path}"
        )

    target = policy.resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {_render_size(len(data))} to {policy.relative(target)}"


def _edit_file(policy: WorkspacePolicy, path: str, old: str, new: str) -> str:
    if old == "":
        raise ToolError("`old` text must be non-empty.")

    target = policy.resolve(path)
    if not target.is_file():
        raise ToolError(f"Not a file: {path}")

    content = target.read_text(encoding="utf-8", errors="replace")
    matches = content.count(old)
    relative = policy.relative(target)
    if matches == 0:
        raise ToolError(f"Text to replace was not found in {relative}.")
    if matches > 1:
        raise ToolError(
            f"Text to replace appears {matches} times in {relative}; it must be unique."
        )

    target.write_text(content.replace(old, new), encoding="utf-8")
    return f"Edited {relative} (replaced 1 occurrence)"


def _search_files(policy: WorkspacePolicy, pattern: str, path: str) -> str:
    base = policy.resolve(path)
    if not base.is_dir():
        raise ToolError(f"Not a directory: {path}")

    matches: list[str] = []
    for candidate in sorted(base.rglob(pattern)):
        try:
            name = policy.relative(candidate)
        except ValueError:
            # A symlink that resolves outside the workspace; skip it silently.
            continue
        if candidate.is_dir():
            name = f"{name}/"
        matches.append(name)
        if len(matches) >= MAX_SEARCH_RESULTS:
            break

    if not matches:
        return f"(no matches for {pattern!r} under {path})"
    return "\n".join(matches)


def _is_secret_file(name: str) -> bool:
    """``.env`` and ``.env.local`` hold secrets; ``.env.example`` is the safe template."""
    return name.startswith(".env") and name != ".env.example"


def _grep_files(
    policy: WorkspacePolicy, pattern: str, path: str, glob: str, ignore_case: bool
) -> str:
    if not pattern:
        raise ToolError("The search pattern is empty.")
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise ToolError(f"Invalid regular expression {pattern!r}: {exc}") from exc

    base = policy.resolve(path)
    if not base.exists():
        raise ToolError(f"No such file or directory: {path}")

    def candidates() -> list[Path]:
        if base.is_file():
            return [base]
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(base):  # symlinked dirs are not followed
            dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS)
            for name in sorted(filenames):
                if fnmatch.fnmatch(name, glob) and not _is_secret_file(name):
                    found.append(Path(dirpath) / name)
        return found

    deadline = time.monotonic() + MAX_GREP_SECONDS
    results: list[str] = []
    note = ""
    for file in candidates():
        try:
            name = policy.relative(file)  # a symlink resolving outside the workspace raises
            size = file.stat().st_size
            if size > MAX_FILE_BYTES:
                continue
            raw = file.read_bytes()
        except (ValueError, OSError):
            continue
        if b"\x00" in raw[:4096]:
            continue  # binary
        for number, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
            if regex.search(line[:2000]):
                text = line.strip("\r")
                if len(text) > _MAX_LINE_CHARS:
                    text = text[: _MAX_LINE_CHARS - 1] + "…"
                results.append(f"{name}:{number}: {text}")
                if len(results) >= MAX_SEARCH_RESULTS:
                    note = (
                        f"(truncated at {MAX_SEARCH_RESULTS} matches: narrow the pattern or path)"
                    )
                    break
        if note:
            break
        if time.monotonic() > deadline:
            note = f"(stopped after {MAX_GREP_SECONDS:.0f}s: narrow the pattern or path)"
            break

    if not results:
        return f"(no matches for {pattern!r} under {path})"
    return "\n".join([*results, note] if note else results)


def filesystem_tools(policy: WorkspacePolicy) -> tuple[BaseTool, ...]:
    """Build the workspace-scoped filesystem tool set for ``policy``."""

    @tool
    async def list_directory(path: str = ".") -> str:
        """List the entries of a directory inside the workspace.

        Files show their size, directories a trailing slash. Use this instead of
        ``ls`` — it is scoped to the workspace and easy to review. ``path`` is
        relative to the workspace root (defaults to the root itself).
        """
        return _list_directory(policy, path)

    @tool
    async def read_file(path: str) -> str:
        """Read a UTF-8 text file as a string. Use this instead of ``cat``/``head``/``tail``.

        Fails if the file does not exist or is larger than the read cap (1 MiB) — for big
        files, search first or read the relevant region. ``path`` is relative to the
        workspace root.
        """
        return _read_file(policy, path)

    @tool
    async def write_file(path: str, content: str) -> str:
        """Create or overwrite a file with the given content. Use this instead of ``echo``.

        Missing parent directories are created; content is capped at 1 MiB. To change
        part of an existing file, prefer ``edit_file``. ``path`` is relative to the
        workspace root and must stay inside it.
        """
        return _write_file(policy, path, content)

    @tool
    async def edit_file(path: str, old: str, new: str) -> str:
        """Replace the single unique occurrence of ``old`` with ``new`` in a file.

        Exact string match, preserving indentation. Rejected if ``old`` appears zero
        times (re-read and retry) or more than once (widen ``old`` with surrounding
        context until it is unique). Prefer this over rewriting a whole file — it sends
        only the diff.
        """
        return _edit_file(policy, path, old, new)

    @tool
    async def search_files(pattern: str, path: str = ".") -> str:
        """Find files by glob name pattern under a directory, e.g. ``pattern="*.py"``.

        Returns up to 200 matches, resolved relative to the workspace root. Use this
        instead of ``find``; it matches file NAMES only. To search what is inside files
        (a TODO, a function name, an error message) use ``grep_files``.
        """
        return _search_files(policy, pattern, path)

    @tool
    async def grep_files(
        pattern: str, path: str = ".", glob: str = "*", ignore_case: bool = False
    ) -> str:
        """Search file CONTENTS for a regular expression. Use this instead of ``grep``/``rg``.

        Returns ``path:line: text`` for each matching line (up to 200). ``path`` is a
        directory or one file, relative to the workspace root; ``glob`` limits which file
        names are searched (e.g. ``"*.py"``). Skips .git, dependencies, caches, binary and
        very large files, and ``.env`` secrets. Examples: ``pattern="TODO|FIXME"``,
        ``pattern="def handle_", glob="*.py"``.
        """
        return await asyncio.to_thread(_grep_files, policy, pattern, path, glob, ignore_case)

    return (list_directory, read_file, write_file, edit_file, search_files, grep_files)
