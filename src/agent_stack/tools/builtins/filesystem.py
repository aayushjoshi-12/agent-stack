"""Filesystem tools — read-only inspection and controlled mutation inside a workspace.

Every tool is a real :class:`~langchain_core.tools.BaseTool` that closes over a
:class:`~agent_stack.core.workspace.WorkspacePolicy`. Paths are always resolved through the
policy, so escapes raise :class:`~agent_stack.core.errors.WorkspaceEscapeError` instead of
touching files outside the workspace. Reads and writes are capped so a tool can never blow
up the model's context by accident.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from agent_stack.core.errors import ToolError
from agent_stack.core.workspace import WorkspacePolicy

__all__ = ["MAX_FILE_BYTES", "MAX_SEARCH_RESULTS", "filesystem_tools"]

#: Largest single file read into or written out of context.
MAX_FILE_BYTES = 1024 * 1024  # 1 MiB

#: Most matches ``search_files`` will return before stopping.
MAX_SEARCH_RESULTS = 200


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


def filesystem_tools(policy: WorkspacePolicy) -> tuple[BaseTool, ...]:
    """Build the workspace-scoped filesystem tool set for ``policy``."""

    @tool
    async def list_directory(path: str = ".") -> str:
        """List the files and directories at ``path`` inside the workspace.

        ``path`` is relative to the workspace root (defaults to the root itself).
        """
        return _list_directory(policy, path)

    @tool
    async def read_file(path: str) -> str:
        """Read a UTF-8 text file at ``path`` (relative to the workspace root).

        Fails if the file does not exist or exceeds the read size cap.
        """
        return _read_file(policy, path)

    @tool
    async def write_file(path: str, content: str) -> str:
        """Create or overwrite the file at ``path`` with ``content``.

        Missing parent directories are created. ``path`` is relative to the workspace
        root and must stay inside it.
        """
        return _write_file(policy, path, content)

    @tool
    async def edit_file(path: str, old: str, new: str) -> str:
        """Replace the single occurrence of ``old`` with ``new`` in the file at ``path``.

        ``old`` must appear exactly once; otherwise the edit is rejected so you never
        change the wrong place by accident.
        """
        return _edit_file(policy, path, old, new)

    @tool
    async def search_files(pattern: str, path: str = ".") -> str:
        """Find files under ``path`` whose name matches the glob ``pattern``.

        For example ``pattern="*.py"``. ``path`` is relative to the workspace root.
        """
        return _search_files(policy, pattern, path)

    return (list_directory, read_file, write_file, edit_file, search_files)
