"""Context assembly and compaction: bound what the model sees.

:class:`ContextBuilder` performs the two jobs Phase 16 needs:

* ``build`` — wrap the last ``recent_window`` messages with an ordered list of
  already-composed system-prompt parts (base prompt, instructions, skills metadata,
  memory summaries), capped to ``max_system_bytes`` (furthest parts dropped first).
* ``compact`` — shrink oversized history to ``summary + recent messages`` when a
  :class:`Summarizer` is available, or to just the recent window when it is not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from langchain_core.messages import BaseMessage, SystemMessage

from agent_stack.core.errors import ConfigurationError

TRUNCATION_MARKER = "…[truncated]"


def truncate_bytes(raw: bytes, limit: int) -> str:
    """Truncate ``raw`` to ``limit`` bytes and append the truncated marker.

    Decoding uses ``errors="replace"`` so an incomplete multi-byte character at the
    cut point becomes U+FFFD rather than raising, and the result is trimmed so its
    UTF-8 encoding never exceeds ``limit`` bytes.
    """
    marker = TRUNCATION_MARKER.encode("utf-8")
    keep = max(0, limit - len(marker))
    text = raw[:keep].decode("utf-8", errors="replace")
    return _trim_to_bytes(text, keep) + TRUNCATION_MARKER


def _trim_to_bytes(text: str, limit: int) -> str:
    """Trim trailing characters until ``text`` encodes within ``limit`` bytes.

    ``errors="replace"`` decoding can overshoot the budget by at most a couple of
    bytes (a partial character becomes a three-byte replacement), so this loop
    terminates after one or two iterations.
    """
    while text and len(text.encode("utf-8")) > limit:
        text = text[:-1]
    return text


@dataclass(frozen=True)
class Context:
    """A bounded model context: ordered system parts plus the message window."""

    system: tuple[str, ...]
    messages: tuple[BaseMessage, ...]


class Summarizer(Protocol):
    """Compresses an old message tail into a summary string."""

    async def summarize(self, messages: Sequence[BaseMessage]) -> str: ...


class ContextBuilder:
    """Assembles bounded contexts and compacts oversized conversation history.

    System parts arrive already composed (base prompt, instructions, skills metadata,
    memory) — the builder does not re-compose them; it only caps the total budget and
    keeps the first (base) part even when it alone exceeds the cap, truncated.
    """

    def __init__(
        self,
        *,
        recent_window: int = 40,
        summarizer: Summarizer | None = None,
        max_system_bytes: int = 64_000,
    ) -> None:
        if recent_window < 1:
            raise ConfigurationError("recent_window must be at least 1")
        if max_system_bytes < 0:
            raise ConfigurationError("max_system_bytes must not be negative")
        self.recent_window = recent_window
        self.summarizer = summarizer
        self.max_system_bytes = max_system_bytes

    def build(self, *, system_parts: Sequence[str], history: Sequence[BaseMessage]) -> Context:
        """Build a context: budget-fit system parts plus the last ``recent_window`` messages."""
        system = tuple(self._fit_system_parts(system_parts))
        messages = tuple(history[-self.recent_window :])
        return Context(system=system, messages=messages)

    async def compact(self, history: list[BaseMessage]) -> list[BaseMessage]:
        """Compress history longer than ``recent_window``; never raises on its own.

        When a summarizer is configured the old tail becomes a leading
        ``SystemMessage``; otherwise the old tail is dropped (v1 strategy).
        """
        if len(history) <= self.recent_window:
            return history
        recent = history[-self.recent_window :]
        if self.summarizer is None:
            return recent
        old = history[: -self.recent_window]
        summary = await self.summarizer.summarize(old)
        return [SystemMessage(content=summary), *recent]

    def _fit_system_parts(self, parts: Sequence[str]) -> list[str]:
        """Keep nearest parts (base prompt first) within ``max_system_bytes``."""
        fitted: list[str] = []
        used = 0
        for index, part in enumerate(parts):
            size = len(part.encode("utf-8"))
            if index == 0:
                if size > self.max_system_bytes:
                    part = truncate_bytes(part.encode("utf-8"), self.max_system_bytes)
                    size = len(part.encode("utf-8"))
                fitted.append(part)
                used = size
                continue
            if used + size > self.max_system_bytes:
                break
            fitted.append(part)
            used += size
        return fitted
