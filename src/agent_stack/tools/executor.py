"""Turn model tool calls into ``ToolExecutionResult``s, concurrently and safely.

A *tool call* is shaped like a LangChain ``AIMessage.tool_calls`` entry::

    {"name": str, "args": dict, "id": str, "type": "tool_call"}

Every failure — unknown tool name or a raising tool — is captured and returned as
an error result rather than propagated, so one bad call never crashes the
model/tool loop.

A :class:`ToolExecutionResult` is the runtime-level outcome of one call; its
``message`` (a :class:`~langchain_core.messages.ToolMessage`) is the model-facing
projection the runtime feeds back into the conversation.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import ToolMessage

from agent_stack.artifacts.base import ArtifactStore
from agent_stack.core.context import truncate_bytes
from agent_stack.core.errors import ToolTimeoutError
from agent_stack.core.run_context import current_run_context
from agent_stack.core.tool_stream import tool_output_sink
from agent_stack.observability.redact import env_secrets, redact
from agent_stack.tools.registry import ToolRegistry

#: Per-call live-output hook: ``(tool_call_id, stream, chunk)``.
ToolOutputHandler = Callable[[str, str, str], Awaitable[None]]

#: Cap on one tool result handed to the model; the rest would only flood its context.
MAX_OUTPUT_BYTES = 100_000
#: With an artifact store, results over this size are stored whole and replaced by a preview.
ARTIFACT_THRESHOLD = 20_000
PREVIEW_BYTES = 4_000

__all__ = [
    "ARTIFACT_THRESHOLD",
    "MAX_OUTPUT_BYTES",
    "ToolExecutionResult",
    "ToolExecutor",
    "ToolOutputHandler",
]


@dataclass(frozen=True)
class ToolExecutionResult:
    """Runtime-level outcome of one tool call.

    Distinct from the model-facing :class:`ToolMessage`, which is its ``message``
    projection: the executor produces results, the runtime feeds ``.message`` to the
    model and records the result for idempotency.
    """

    tool_call_id: str
    tool_name: str
    message: ToolMessage
    success: bool
    error: str | None = None


class ToolExecutor:
    """Executes tool calls against a :class:`ToolRegistry`."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        timeout_seconds: float | None = None,
        max_output_bytes: int = MAX_OUTPUT_BYTES,
        artifacts: ArtifactStore | None = None,
        artifact_threshold: int = ARTIFACT_THRESHOLD,
    ) -> None:
        self.registry = registry
        #: Per-call timeout; ``None`` means a call may run as long as it likes.
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self._artifacts = artifacts
        self.artifact_threshold = artifact_threshold
        self._secrets = env_secrets()

    async def _for_model(self, text: str, tool_name: str) -> str:
        """What the model may see of a tool's result: no known secrets, and short.

        With an artifact store, a result over ``artifact_threshold`` is stored in full (the
        already-redacted text) and the model gets a preview plus ``artifact://<id>``; without
        one, or if storing fails, it is truncated at ``max_output_bytes``.
        """
        text = redact(text, self._secrets, pairs=False)
        raw = text.encode("utf-8")
        if self._artifacts is not None and len(raw) > self.artifact_threshold:
            context = current_run_context()
            with contextlib.suppress(Exception):  # fall back to plain truncation below
                ref = await self._artifacts.put(
                    data=raw,
                    name=f"{tool_name}-output.txt",
                    mime_type="text/plain",
                    session_id=context.session_id if context else None,
                )
                return (
                    f"{truncate_bytes(raw, PREVIEW_BYTES)}\n"
                    f"[output was {len(raw)} bytes; full output stored as {ref.uri}; "
                    "read it in pieces with read_artifact]"
                )
        if len(raw) <= self.max_output_bytes:
            return text
        return truncate_bytes(raw, self.max_output_bytes)

    async def execute_one(
        self,
        tool_call: Mapping[str, Any],
        *,
        on_output: ToolOutputHandler | None = None,
    ) -> ToolExecutionResult:
        """Execute a single tool call, returning a result either way.

        Every call runs with a per-call sink installed, so a streaming tool can emit live
        output through :func:`current_tool_output_sink`; ``on_output`` is an optional
        ``(tool_call_id, stream, chunk)`` hook that receives it. A call that outlives the
        timeout is cancelled (``run_command`` kills its process group on cancellation) and
        becomes an error result carrying the output so far. A tool may set its own bound in
        ``metadata["timeout_seconds"]`` (``None``: none).
        """
        tool_call_id: str = tool_call["id"]
        tool_name = str(tool_call["name"])

        # What a streaming tool emitted so far: reported if the call is cut off.
        captured: list[str] = []
        captured_bytes = 0

        async def sink(stream: str, chunk: str) -> None:
            nonlocal captured_bytes
            if captured_bytes < self.max_output_bytes:
                captured.append(chunk)
                captured_bytes += len(chunk.encode("utf-8"))
            if on_output is not None:
                await on_output(tool_call_id, stream, chunk)

        timeout = self.timeout_seconds
        deadline = asyncio.timeout(None)
        try:
            tool = self.registry.get(tool_name)
            metadata = getattr(tool, "metadata", None) or {}
            if "timeout_seconds" in metadata:
                timeout = metadata["timeout_seconds"]  # a tool with its own bound
            deadline = asyncio.timeout(timeout)
            async with deadline:
                with tool_output_sink(sink):
                    # Invoke with the *arguments*, never the whole tool_call dict.
                    result = await tool.ainvoke(tool_call["args"])
        except Exception as exc:
            if isinstance(exc, TimeoutError) and deadline.expired():
                so_far = "".join(captured)
                exc = ToolTimeoutError(
                    f"tool '{tool_name}' timed out after {timeout:g}s"
                    + (f"; output so far:\n{so_far}" if so_far else "")
                )
            return ToolExecutionResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                message=ToolMessage(
                    content=await self._for_model(f"Error: {exc}", tool_name),
                    tool_call_id=tool_call_id,
                    status="error",
                ),
                success=False,
                error=str(exc),
            )

        return ToolExecutionResult(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            message=ToolMessage(
                content=await self._for_model(str(result), tool_name), tool_call_id=tool_call_id
            ),
            success=True,
        )

    async def execute_many(
        self,
        tool_calls: Sequence[Mapping[str, Any]],
        *,
        on_output: ToolOutputHandler | None = None,
    ) -> list[ToolExecutionResult]:
        """Execute every call concurrently, preserving the input order.

        ``on_output`` is forwarded to each call; ``asyncio.gather`` runs each in its own
        task, so the per-call sink stays isolated.
        """
        if not tool_calls:
            return []

        results = await asyncio.gather(
            *(self.execute_one(tool_call, on_output=on_output) for tool_call in tool_calls)
        )
        return list(results)
