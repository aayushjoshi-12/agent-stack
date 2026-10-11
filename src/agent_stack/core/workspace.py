"""Workspace confinement for filesystem and terminal tools.

Every path a tool touches is funnelled through a :class:`WorkspacePolicy`, which resolves
it against a single root directory and rejects anything that escapes — including symlinks
and absolute inputs pointing elsewhere. This is the one place that decides what "inside the
workspace" means, so the tools themselves stay boring.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_stack.core.errors import ConfigurationError, WorkspaceEscapeError

__all__ = ["WorkspacePolicy"]


@dataclass(frozen=True)
class WorkspacePolicy:
    """A confined root directory that paths may be resolved against.

    ``root`` is normalized to an absolute, symlink-resolved directory on construction.
    When ``allow_external`` is ``False`` (the default) any path resolving outside ``root``
    raises :class:`WorkspaceEscapeError`.
    """

    root: Path
    allow_external: bool = False

    def __post_init__(self) -> None:
        root = Path(self.root).expanduser().resolve()
        object.__setattr__(self, "root", root)
        if not root.is_dir():
            raise ConfigurationError(f"Workspace root is not a directory: {root}")

    def resolve(self, path: str | Path) -> Path:
        """Resolve ``path`` against the root, rejecting escapes.

        Absolute inputs are allowed only when they already live inside the root. Relative
        inputs are joined to the root first. The result is fully resolved (symlinks
        followed) so ``..`` tricks and symlink hops cannot slip past the check.
        """
        candidate = Path(path)
        base = candidate if candidate.is_absolute() else self.root / candidate
        resolved = base.resolve()

        if self.allow_external or resolved.is_relative_to(self.root):
            return resolved

        raise WorkspaceEscapeError(f"Path {path!s} resolves outside workspace {self.root}")

    def relative(self, path: Path) -> str:
        """Return ``path`` as a POSIX string relative to the root."""
        target = path if path.is_absolute() else self.root / path
        return target.resolve().relative_to(self.root).as_posix()
