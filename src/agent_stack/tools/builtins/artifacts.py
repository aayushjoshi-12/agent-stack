"""``read_artifact``: page through an oversized tool result stored as an artifact (P15)."""

from __future__ import annotations

import re

from langchain_core.tools import BaseTool, tool

from agent_stack.artifacts.base import ArtifactRef, ArtifactStore
from agent_stack.core.errors import ArtifactError

__all__ = ["artifact_tools"]

#: Artifact ids are a hex uuid plus an optional short extension; anything else is refused
#: before it reaches the store.
_ID = re.compile(r"^[0-9a-f]{32}(\.[A-Za-z0-9]{1,10})?$")
_DEFAULT_LIMIT = 20_000


def artifact_tools(store: ArtifactStore) -> tuple[BaseTool, ...]:
    """Build the ``read_artifact`` tool bound to ``store``."""

    @tool
    async def read_artifact(id: str, offset: int = 0, limit: int = _DEFAULT_LIMIT) -> str:
        """Read part of a stored tool output (an ``artifact://<id>`` from an earlier result).

        Returns ``limit`` bytes starting at byte ``offset``; call again with a larger offset
        to continue. Read only the pieces you need.
        """
        artifact_id = id.removeprefix("artifact://").strip()
        if not _ID.match(artifact_id):
            return f"Error: {id!r} is not a valid artifact id."
        ref = ArtifactRef(
            id=artifact_id,
            name=artifact_id,
            mime_type="text/plain",
            size_bytes=0,
            uri=f"artifact://{artifact_id}",
        )
        try:
            data = await store.get(ref)
        except ArtifactError as exc:
            return f"Error: {exc}"
        start = max(0, offset)
        end = start + max(1, min(limit, _DEFAULT_LIMIT * 4))
        piece = data[start:end].decode("utf-8", errors="replace")
        return f"[bytes {start}-{min(end, len(data))} of {len(data)}]\n{piece}"

    return (read_artifact,)
