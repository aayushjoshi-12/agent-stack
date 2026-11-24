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

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage

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
    #: The builder's own (uncalibrated) token estimate of this context, for ``calibrate``.
    raw_estimate: int = 0
    #: Messages left out to fit the token budget (oldest first), so a caller can summarise them.
    dropped: tuple[BaseMessage, ...] = ()


class Summarizer(Protocol):
    """Compresses an old message tail into a summary string."""

    async def summarize(self, messages: Sequence[BaseMessage]) -> str: ...


class SystemPartsProvider(Protocol):
    """Supplies the ordered resident system-prompt parts for a run.

    ``parts`` returns already-composed resident blocks in order — the base prompt first,
    then instructions, skills metadata, and memory — for the runtime to hand to
    :meth:`ContextBuilder.build`. ``core`` only knows this protocol; ``context``
    implements it, so the dependency between the two stays one-way.
    """

    async def parts(self, *, base_prompt: str) -> Sequence[str]: ...


#: Room kept for the "earlier messages were dropped" note, so the budget still holds with it.
_NOTE_TOKENS = 60


def estimate_tokens(messages: Sequence[BaseMessage], extra_chars: int = 0) -> int:
    """A cheap token estimate (about four characters per token), tool-call arguments included."""
    chars = extra_chars
    for message in messages:
        chars += len(str(message.content))
        for call in getattr(message, "tool_calls", None) or ():
            chars += len(str(call.get("args", "")))
    return chars // 4


def _cleared(message: ToolMessage) -> ToolMessage:
    """A stand-in for an old tool result: same call id (the pair stays valid), no payload."""
    size = len(str(message.content))
    note = f"[output cleared to save context ({size} chars); run the tool again if you need it]"
    return ToolMessage(content=note, tool_call_id=message.tool_call_id, name=message.name)


def _without_leading_tool_results(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """Drop tool results at the front whose tool call is no longer in the window."""
    rest = list(messages)
    while rest and isinstance(rest[0], ToolMessage):
        rest.pop(0)
    return rest


def _recent(history: Sequence[BaseMessage], window: int) -> list[BaseMessage]:
    """The last ``window`` messages, never starting on an orphaned tool result.

    A cut that lands between an assistant message and its tool results would leave
    ToolMessages whose tool call the model can no longer see, which providers reject.
    """
    return _without_leading_tool_results(history[-window:])


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
        max_tokens: int | None = None,
        keep_tool_results: int = 6,
    ) -> None:
        if recent_window < 1:
            raise ConfigurationError("recent_window must be at least 1")
        if max_system_bytes < 0:
            raise ConfigurationError("max_system_bytes must not be negative")
        self.recent_window = recent_window
        self.summarizer = summarizer
        self.max_system_bytes = max_system_bytes
        self.max_tokens = max_tokens
        self.keep_tool_results = keep_tool_results
        #: Real tokens per estimated token, learned from the provider's reported usage.
        self.scale = 1.0

    def build(
        self,
        *,
        system_parts: Sequence[str],
        history: Sequence[BaseMessage],
        pinned: BaseMessage | None = None,
        summary: str | None = None,
    ) -> Context:
        """Build a context: budget-fit system parts plus a bounded message window.

        ``pinned`` (the request this run is working on) always stays first, however long the
        run gets. With ``max_tokens`` set, an over-budget window first clears the payload of
        old tool results (keeping the newest ``keep_tool_results``), and only then drops the
        oldest messages. Stored messages are never changed; the model sees copies.
        """
        system = tuple(self._fit_system_parts(system_parts))
        messages = _recent(history, self.recent_window)
        if pinned is not None and pinned not in messages:
            messages = [pinned, *_without_leading_tool_results(messages)]
        dropped: list[BaseMessage] = []
        if self.max_tokens is not None:
            messages, dropped = self._fit_tokens(system, messages, pinned, summary)
        raw = estimate_tokens(messages, sum(len(part) for part in system))
        return Context(
            system=system, messages=tuple(messages), raw_estimate=raw, dropped=tuple(dropped)
        )

    def calibrate(self, raw_estimate: int, actual_tokens: int) -> None:
        """Learn how far the estimate is off from the provider's reported input tokens.

        The ratio is smoothed and kept between 1 and 3: it only ever makes the budget more
        careful, and one odd reading cannot swing it.
        """
        if raw_estimate <= 0 or actual_tokens <= 0:
            return
        ratio = min(3.0, max(1.0, actual_tokens / raw_estimate))
        self.scale = 0.5 * self.scale + 0.5 * ratio

    def _fit_tokens(
        self,
        system: tuple[str, ...],
        messages: list[BaseMessage],
        pinned: BaseMessage | None,
        summary: str | None = None,
    ) -> tuple[list[BaseMessage], list[BaseMessage]]:
        assert self.max_tokens is not None
        system_chars = sum(len(part) for part in system)

        def size(items: Sequence[BaseMessage]) -> int:
            return int(estimate_tokens(items, system_chars) * self.scale)

        if size(messages) <= self.max_tokens:
            return messages, []
        tool_positions = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
        for i in tool_positions[: max(0, len(tool_positions) - self.keep_tool_results)]:
            messages[i] = _cleared(messages[i])  # type: ignore[arg-type]
        head = [pinned] if pinned is not None and messages and messages[0] is pinned else []
        rest = messages[len(head) :]
        before = len(rest)
        reserve = max(_NOTE_TOKENS, len(summary or "") // 3)
        while len(rest) > 1 and size([*head, *rest]) + reserve > self.max_tokens:
            rest = _without_leading_tool_results(rest[1:])
        if len(rest) < before:  # say so: a model that is not told re-plans from scratch
            gone = before - len(rest)
            dropped = list(messages[len(head) : len(head) + gone])
            text = (
                f"[{gone} earlier messages were dropped to fit the context. "
                "Your todo list, notes and the files on disk still show your progress; "
                "check them instead of starting over.]"
            )
            if summary:
                text += f"\n\nSummary of what happened in them:\n{summary}"
            return [*head, HumanMessage(content=text), *rest], dropped
        return [*head, *rest], []

    async def compact(self, history: list[BaseMessage]) -> list[BaseMessage]:
        """Compress history longer than ``recent_window``; never raises on its own.

        When a summarizer is configured the old tail becomes a leading
        ``SystemMessage``; otherwise a one-line note says how many messages were omitted.
        """
        if len(history) <= self.recent_window:
            return history
        recent = _recent(history, self.recent_window)
        old = history[: len(history) - len(recent)]
        if self.summarizer is None:
            return [SystemMessage(content=f"[{len(old)} earlier messages were omitted.]"), *recent]
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
