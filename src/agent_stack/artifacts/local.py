"""Filesystem-backed artifact store.

Bytes are written under ``root/<id>`` where ``id`` is a fresh UUID. The stored
id — never the caller-supplied ``name`` — determines the on-disk path, so a
malicious name cannot escape ``root``.
"""

from __future__ import annotations

import os
import re
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from agent_stack.artifacts.base import DEFAULT_MIME, ArtifactRef, guess_mime_type
from agent_stack.core.errors import ArtifactError

_SAFE_EXTENSION = re.compile(r"^[A-Za-z0-9]{1,10}$")


def _sanitize_name(name: str) -> str:
    """Reduce ``name`` to a bare, safe label (no directories, no ``..``)."""
    # Treat both separators uniformly, then keep only the final component.
    candidate = PurePosixPath(name.replace("\\", "/")).name.strip()
    # A bare ".."/"." (or empty) is not a usable label.
    if candidate in {"", ".", ".."}:
        return "artifact"
    return candidate


def _safe_extension(name: str) -> str:
    """Return a leading-dot extension safe to append to a UUID, or ``""``."""
    extension = Path(name).suffix
    if extension.startswith(".") and _SAFE_EXTENSION.match(extension[1:]):
        return extension
    return ""


class LocalArtifactStore:
    """Store artifacts as files beneath ``root``."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        """The directory artifacts are stored under."""
        return self._root

    def _path_for_id(self, artifact_id: str) -> Path:
        """Resolve ``artifact_id`` to a path, rejecting anything escaping root."""
        # ids are UUIDs we generate, but a ref may be caller-supplied: never
        # trust it to be a simple filename.
        resolved = (self._root / artifact_id).resolve()
        root = self._root.resolve()
        if not resolved.is_relative_to(root) or resolved == root:
            raise ArtifactError(f"Invalid artifact id: {artifact_id!r}")
        return resolved

    async def put(
        self,
        *,
        data: bytes,
        name: str,
        mime_type: str = DEFAULT_MIME,
        session_id: str | None = None,
    ) -> ArtifactRef:
        """Write ``data`` to a UUID-named file and return its reference."""
        del session_id  # Local store has no per-session layout yet.
        safe_name = _sanitize_name(name)
        artifact_id = f"{uuid.uuid4().hex}{_safe_extension(safe_name)}"
        target = self._path_for_id(artifact_id)

        self._root.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_path = tempfile.mkstemp(dir=self._root, prefix=".tmp-")
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(data)
            os.replace(temp_path, target)
        except OSError as exc:
            Path(temp_path).unlink(missing_ok=True)
            raise ArtifactError(f"Failed to store artifact {artifact_id!r}: {exc}") from exc

        return ArtifactRef(
            id=artifact_id,
            name=safe_name,
            mime_type=mime_type or guess_mime_type(safe_name),
            size_bytes=len(data),
            uri=f"artifact://{artifact_id}",
        )

    async def get(self, ref: ArtifactRef) -> bytes:
        """Return the stored bytes; raise :class:`ArtifactError` if absent."""
        path = self._path_for_id(ref.id)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactError(f"Artifact not found: {ref.id!r}") from exc

    async def delete(self, ref: ArtifactRef) -> None:
        """Remove the artifact; deleting a missing one is a no-op (idempotent)."""
        path = self._path_for_id(ref.id)
        try:
            path.unlink()
        except FileNotFoundError:
            return

    async def put_text(
        self,
        *,
        text: str,
        name: str,
        session_id: str | None = None,
    ) -> ArtifactRef:
        """Convenience wrapper storing UTF-8 text, guessing MIME from ``name``."""
        safe_name = _sanitize_name(name)
        return await self.put(
            data=text.encode("utf-8"),
            name=safe_name,
            mime_type=guess_mime_type(safe_name),
            session_id=session_id,
        )
