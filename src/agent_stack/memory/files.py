"""File-based memory: the ``MEMORY.md`` index and its topic files (P16d).

A memory root holds an index and one file per topic::

    <memory root>/
    ├── MEMORY.md            # index: one short line per topic
    └── topics/
        └── <topic-name>.md  # full topic content

The same three-level progressive disclosure as skills (P16b): the index is resident in
the system prompt (level 1), a topic body is read on demand (level 2), and a topic is
written on demand, keeping its index line in sync (level 3).

The root is ``<workspace>/.agent/memory/``; when that has no index, a bare
``<workspace>/MEMORY.md`` is used instead. Nothing is created at read time — the
directory and index appear only when a topic is written.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from agent_stack.core.context import truncate_bytes
from agent_stack.core.errors import AgentStackError

__all__ = ["MAX_INDEX_BYTES", "MemoryFilesError", "MemoryIndex"]

#: Byte cap for the resident index, mirroring the instruction/skill-metadata budget.
MAX_INDEX_BYTES = 8192

#: Leading separator for an index line: ``TopicName — one-line summary``.
_INDEX_SEPARATOR = " — "

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class MemoryFilesError(AgentStackError):
    """Raised when a memory root, topic name, or topic file is invalid or missing."""


def _slugify(name: str) -> str:
    """Lowercase a topic name into a filename-safe slug (``My Topic`` → ``my-topic``)."""
    return _SLUG_RE.sub("-", name.strip().lower()).strip("-")


def _summarize(content: str) -> str:
    """A one-line summary for the index: the first non-heading line, else first heading."""
    heading = ""
    for line in content.splitlines():
        text = line.strip()
        if not text:
            continue
        if text.startswith("#"):
            heading = heading or text.lstrip("#").strip()
            continue
        return text[:100]
    return heading[:100]


def _body(text: str) -> str:
    """Strip a leading ``---`` frontmatter block, returning the topic body."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4 :].lstrip("\n")


class MemoryIndex:
    """The ``MEMORY.md`` index plus on-demand topic reads and writes for one workspace."""

    def __init__(self, workspace: Path, *, max_index_bytes: int = MAX_INDEX_BYTES) -> None:
        self._workspace = Path(workspace).expanduser().resolve()
        self._root = self._resolve_root()
        self._max_index_bytes = max_index_bytes

    @property
    def root(self) -> Path:
        """The resolved memory root (not created until a topic is written)."""
        return self._root

    def _resolve_root(self) -> Path:
        """Prefer ``.agent/memory/``; fall back to a bare workspace ``MEMORY.md``."""
        primary = self._workspace / ".agent" / "memory"
        if (primary / "MEMORY.md").is_file():
            return primary
        if (self._workspace / "MEMORY.md").is_file():
            return self._workspace
        return primary

    def index(self) -> str:
        """The capped resident index text, or ``""`` when there is no index."""
        path = self._root / "MEMORY.md"
        if not path.is_file():
            return ""
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        if len(raw) > self._max_index_bytes:
            return truncate_bytes(raw, self._max_index_bytes)
        return raw.decode("utf-8", errors="replace").strip()

    def read_topic(self, name: str) -> str:
        """Return the body of topic ``name`` (level 2); unknown topics raise."""
        path = self._topic_path(name)
        if not path.is_file():
            raise MemoryFilesError(f"No such memory topic: {name!r}")
        try:
            return _body(path.read_text(encoding="utf-8", errors="replace")).rstrip("\n")
        except OSError as exc:
            raise MemoryFilesError(f"Could not read memory topic {name!r}: {exc}") from exc

    def write_topic(self, name: str, content: str) -> Path:
        """Create or update topic ``name`` and keep its ``MEMORY.md`` line in sync (level 3)."""
        path = self._topic_path(name)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            updated = datetime.now(UTC).isoformat()
            path.write_text(
                f"---\nupdated: {updated}\n---\n\n{content.rstrip()}\n", encoding="utf-8"
            )
            self._write_index(name, _summarize(content))
        except OSError as exc:
            raise MemoryFilesError(f"Could not write memory topic {name!r}: {exc}") from exc
        return path

    def _topic_path(self, name: str) -> Path:
        """Resolve ``name`` to ``topics/<slug>.md``, rejecting anything path-like."""
        if not name or not name.strip():
            raise MemoryFilesError("Topic name must be non-empty.")
        if Path(name).is_absolute() or ".." in name or "/" in name or "\\" in name:
            raise MemoryFilesError(f"Invalid topic name (must not be a path): {name!r}")
        slug = _slugify(name)
        if not slug:
            raise MemoryFilesError(f"Invalid topic name: {name!r}")
        resolved = (self._root / "topics" / f"{slug}.md").resolve()
        if not resolved.is_relative_to(self._root):
            raise MemoryFilesError(f"Topic {name!r} escapes memory directory {self._root}")
        return resolved

    def _write_index(self, name: str, summary: str) -> None:
        """Update ``name``'s index line in place, or append it when new."""
        path = self._root / "MEMORY.md"
        try:
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError:
            existing = ""

        line = f"{name.strip()}{_INDEX_SEPARATOR}{summary}" if summary else name.strip()
        slug = _slugify(name)
        lines = existing.splitlines()
        for position, current in enumerate(lines):
            label = current.partition(_INDEX_SEPARATOR)[0]
            if _slugify(label) == slug:
                lines[position] = line
                break
        else:
            lines.append(line)

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines).strip("\n") + "\n", encoding="utf-8")
