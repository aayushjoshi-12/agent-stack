"""Agent instruction loading: resolve ``AGENTS.md`` / ``CLAUDE.md`` nearest-first.

Implements the Phase 16c contract. Starting at the target directory, walk *up* toward
``stop_at`` (or the filesystem root when ``None``, stopping before ascending past it).
In each directory prefer ``AGENTS.md``; fall back to ``CLAUDE.md`` when it is absent.
Return the files nearest-first, capping each file at ``max_file_bytes`` (truncated with
a marker) and the accumulated total at ``max_total_bytes`` (furthest files dropped).

Loading never raises for missing files; a file that exists but cannot be read raises
:class:`InstructionsError`. Reads are cached by ``(path, mtime)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_stack.core.context import truncate_bytes
from agent_stack.core.errors import InstructionsError


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

    def __init__(self, *, max_file_bytes: int = 8192, max_total_bytes: int = 32768) -> None:
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self._cache: dict[Path, tuple[float, InstructionFile]] = {}

    async def load(self, target_dir: Path, stop_at: Path | None = None) -> list[InstructionFile]:
        """Load every instructions file from ``target_dir`` up to ``stop_at``, nearest first."""
        start = target_dir.resolve()
        stop = stop_at.resolve() if stop_at is not None else None

        files: list[InstructionFile] = []
        total = 0
        for directory in self._directories_up_to(start, stop):
            candidate = self._pick(directory)
            if candidate is None:
                continue
            instruction = self._load_cached(candidate)
            effective = min(instruction.size_bytes, self.max_file_bytes)
            if total + effective > self.max_total_bytes:
                break  # furthest files dropped; partial fits allowed
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

    def _pick(self, directory: Path) -> Path | None:
        """Choose the instructions file for one directory, or ``None`` when absent."""
        agents = directory / "AGENTS.md"
        if agents.exists():
            return agents
        claude = directory / "CLAUDE.md"
        if claude.exists():
            return claude
        return None

    def _load_cached(self, path: Path) -> InstructionFile:
        """Return the cached file when its mtime is unchanged, else read and cache it."""
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            raise InstructionsError(f"cannot stat instruction file {path}: {exc}") from exc
        cached = self._cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise InstructionsError(f"cannot read instruction file {path}: {exc}") from exc
        instruction = self._build(path, raw)
        self._cache[path] = (mtime, instruction)
        return instruction

    def _build(self, path: Path, raw: bytes) -> InstructionFile:
        """Build an :class:`InstructionFile`, truncating oversized content in place."""
        size = len(raw)
        if size > self.max_file_bytes:
            content = truncate_bytes(raw, self.max_file_bytes)
        else:
            content = raw.decode("utf-8", errors="replace")
        return InstructionFile(path=path, content=content, size_bytes=size)