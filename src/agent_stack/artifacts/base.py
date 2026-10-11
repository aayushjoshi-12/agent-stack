"""Artifact definitions and the store protocol.

An :class:`ArtifactRef` is the compact, serializable pointer the runtime hands
to the model instead of the (potentially huge) payload itself. The bytes live
behind an :class:`ArtifactStore` implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

DEFAULT_MIME = "application/octet-stream"

#: Extension → MIME type for the handful of text-ish types we care about.
_MIME_BY_EXTENSION: dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".json": "application/json",
    ".log": "text/plain",
}


def guess_mime_type(name: str) -> str:
    """Best-effort MIME type from a file ``name``'s extension.

    Falls back to ``application/octet-stream`` for anything unrecognized.
    """
    _, _, extension = name.rpartition(".")
    if not extension:
        return DEFAULT_MIME
    return _MIME_BY_EXTENSION.get(f".{extension.lower()}", DEFAULT_MIME)


@dataclass(frozen=True)
class ArtifactRef:
    """A pointer to a stored artifact.

    ``id`` is authoritative on disk; ``name`` is a human-friendly label.
    """

    id: str
    name: str
    mime_type: str
    size_bytes: int
    uri: str


@runtime_checkable
class ArtifactStore(Protocol):
    """Store large payloads and return compact references to them."""

    async def put(
        self,
        *,
        data: bytes,
        name: str,
        mime_type: str = DEFAULT_MIME,
        session_id: str | None = None,
    ) -> ArtifactRef:
        """Persist ``data`` and return a reference to it."""
        ...

    async def get(self, ref: ArtifactRef) -> bytes:
        """Return the bytes for ``ref``; raise :class:`ArtifactError` if missing."""
        ...

    async def delete(self, ref: ArtifactRef) -> None:
        """Remove the artifact for ``ref``."""
        ...
