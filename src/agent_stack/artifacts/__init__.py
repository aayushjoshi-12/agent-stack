"""Artifact storage: keep large payloads out of the model context.

Artifacts are addressed by an opaque id and surfaced to callers as an
``artifact://<id>`` URI (see :class:`~agent_stack.artifacts.base.ArtifactRef`).
"""

from __future__ import annotations

from agent_stack.artifacts.base import DEFAULT_MIME, ArtifactRef, ArtifactStore, guess_mime_type
from agent_stack.artifacts.local import LocalArtifactStore

__all__ = [
    "DEFAULT_MIME",
    "ArtifactRef",
    "ArtifactStore",
    "LocalArtifactStore",
    "guess_mime_type",
]
