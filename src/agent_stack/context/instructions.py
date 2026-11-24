"""Agent instruction loading: resolve ``AGENTS.md`` / ``CLAUDE.md`` nearest-first.

Implements the Phase 16c contract. Starting at the target directory, walk *up* toward
``stop_at`` (or the filesystem root when ``None``, stopping before ascending past it).
In each directory prefer ``AGENTS.md``; fall back to ``CLAUDE.md`` when it is absent.
Return the files nearest-first, capping each file at ``max_file_bytes`` (truncated with
a marker) and the accumulated total at ``max_total_bytes`` (furthest files dropped).

Each directory contributes its ``AGENTS.local.md`` / ``CLAUDE.local.md`` (private, gitignored
overlays) ahead of its shared file, and ``@relative/path.md`` import lines inside the shared file
are inlined (inside that directory only, three levels deep). An optional ``global_dir`` adds a
user-level ``AGENTS.md`` as the furthest layer. A truncated file is logged, never silent.

Loading never raises for missing files; a file that exists but cannot be read raises
:class:`InstructionsError`. Reads are cached by ``(path, mtime)``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from agent_stack.core.context import truncate_bytes
from agent_stack.core.errors import InstructionsError

_log = logging.getLogger(__name__)
_IMPORT_RE = re.compile(r"^@(\S+\.md)\s*$", re.MULTILINE)
_LOCAL_NAMES = ("AGENTS.local.md", "CLAUDE.local.md")
_MAX_IMPORT_DEPTH = 3


@dataclass(frozen=True)
class InstructionFile:
    """One resolved instructions file: its path, loaded content, and raw size."""

    path: Path
    content: str
    size_bytes: int


class InstructionsLoader:
    """Discovers instructions files from a directory upward, nearest first.

    ``AGENTS.md`` beats ``CLAUDE.md`` when both exist in the same directory, and a
    directory with neither contributes nothing.
    """

    def __init__(
        self,
        *,
        max_file_bytes: int = 32768,
        max_total_bytes: int = 65536,
        global_dir: Path | None = None,
    ) -> None:
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.global_dir = global_dir
        self._cache: dict[Path, tuple[float, InstructionFile]] = {}

    async def load(self, target_dir: Path, stop_at: Path | None = None) -> list[InstructionFile]:
        """Load every instructions file from ``target_dir`` up to ``stop_at``, nearest first."""
        start = target_dir.resolve()
        stop = stop_at.resolve() if stop_at is not None else None

        files: list[InstructionFile] = []
        total = 0
        directories = self._directories_up_to(start, stop)
        if self.global_dir is not None:
            directories.append(self.global_dir.expanduser().resolve())
        for directory in directories:
            for candidate in self._pick(directory):
                instruction = self._load_cached(candidate)
                effective = min(instruction.size_bytes, self.max_file_bytes)
                if total + effective > self.max_total_bytes:
                    _log.warning("instructions over budget; dropped %s and the rest", candidate)
                    return files  # furthest files dropped
                files.append(instruction)
                total += effective
        return files

    def _directories_up_to(self, start: Path, stop: Path | None) -> list[Path]:
        """Directories from ``start`` up to (and including) ``stop`` or the root."""
        directories: list[Path] = []
        current = start
        while True:
            directories.append(current)
            if current == stop:
                break
            parent = current.parent
            if parent == current:
                break  # filesystem root
            current = parent
        if stop is not None:
            directories = [d for d in directories if d.is_relative_to(stop)]
        return directories

    def _pick(self, directory: Path) -> list[Path]:
        """Local overlays first, then ``AGENTS.md`` (else ``CLAUDE.md``); empty when absent."""
        picked = [p for name in _LOCAL_NAMES if (p := directory / name).is_file()]
        for name in ("AGENTS.md", "CLAUDE.md"):
            if (directory / name).exists():
                picked.append(directory / name)
                break
        return picked

    def _load_cached(self, path: Path) -> InstructionFile:
        """Return the cached file when its mtime is unchanged, else read and cache it."""
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise InstructionsError(f"cannot stat instruction file {path}: {exc}") from exc
        cached = self._cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        raw = self._read_with_imports(path, path.parent, 0, frozenset())
        instruction = self._build(path, raw)
        self._cache[path] = (mtime, instruction)
        return instruction

    def _read_with_imports(
        self, path: Path, boundary: Path, depth: int, seen: frozenset[Path]
    ) -> bytes:
        """Read ``path``, inlining ``@file.md`` lines that stay inside ``boundary``."""
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            raise InstructionsError(f"cannot read instruction file {path}: {exc}") from exc
        seen = seen | {path.resolve()}

        def inline(match: re.Match[str]) -> str:
            target = (path.parent / match.group(1)).resolve()
            if depth >= _MAX_IMPORT_DEPTH or target in seen or not target.is_file():
                return match.group(0)
            if not target.is_relative_to(boundary.resolve()):
                return match.group(0)
            return self._read_with_imports(target, boundary, depth + 1, seen).decode()

        return _IMPORT_RE.sub(inline, text).encode("utf-8")

    def _build(self, path: Path, raw: bytes) -> InstructionFile:
        """Build an :class:`InstructionFile`, truncating oversized content in place."""
        size = len(raw)
        if size > self.max_file_bytes:
            _log.warning("%s is %d bytes; truncated to %d", path, size, self.max_file_bytes)
            content = truncate_bytes(raw, self.max_file_bytes)
        else:
            content = raw.decode("utf-8", errors="replace")
        return InstructionFile(path=path, content=content, size_bytes=size)
