"""The gateway contract: one inbound shape, one outbound protocol.

Every gateway converts its native protocol into a :class:`MessageEnvelope`,
drives the :class:`~agent_stack.app.application.Application` with it, and renders
the :class:`AgentEvent` stream that comes back. This module is the only place
that contract is spelled out; concrete gateways (CLI, TUI, Telegram, Web) live in
their own submodules.

The identity seam — ``gateway identity → principal → session`` — is provided by
the application's resolvers (:class:`IdentityResolver` /
:class:`SessionResolver`); a gateway uses them to build an envelope whose
``principal`` and ``session`` are already resolved.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from agent_stack.core.events import AgentEvent
from agent_stack.core.images import IMAGE_SUFFIXES, Image, load_image
from agent_stack.core.images import MAX_IMAGE_BYTES as MAX_IMAGE_BYTES
from agent_stack.core.images import make_image as make_image  # for gateways (P44)
from agent_stack.core.images import sniff_mime as sniff_mime

if TYPE_CHECKING:
    from agent_stack.app.application import Application

__all__ = [
    "Gateway",
    "HistoryItem",
    "Image",
    "MessageEnvelope",
    "SessionSummary",
    "SessionUsage",
    "SlashCommand",
    "format_cost",
    "format_tokens",
    "format_when",
    "image_paths",
    "load_images",
    "model_breakdown",
    "usage_summary",
]


@dataclass(frozen=True)
class MessageEnvelope:
    """One inbound message, already resolved to the application boundary.

    ``principal`` is the resolved owner id, ``session`` the session to run in
    (the runtime creates the session lazily on first use), and ``text`` the
    message. ``images`` are attached to it (P44), already read and checked by the gateway.
    ``metadata`` carries opaque gateway extras (idempotency keys, native message ids) that no
    consumer needs yet.
    """

    principal: str
    session: str
    text: str
    metadata: Mapping[str, Any] = field(default_factory=dict)
    images: tuple[Image, ...] = ()


@dataclass(frozen=True)
class SessionSummary:
    """One conversation in a picker: its id and a human title (first message)."""

    id: str
    title: str
    updated_at: str | None = None  # ISO, UTC: when it was last active
    created_at: str | None = None
    branch_of: str | None = None


@dataclass(frozen=True)
class SlashCommand:
    """One ``/name`` a user can type: a skill, or a built-in action of the gateway."""

    name: str
    description: str
    kind: str = "skill"  # "skill" or "builtin"


def format_when(iso: str | None, *, now: datetime | None = None) -> str:
    """A timestamp for people, in local time: ``today 14:05``, ``yesterday 09:30``, ``14 Nov 07:21``
    (with the year when it is not this year). Empty for a missing or unreadable one."""
    if not iso:
        return ""
    try:
        when = datetime.fromisoformat(iso).astimezone()
    except ValueError:
        return ""
    current = (now or datetime.now().astimezone()).astimezone()
    days = (current.date() - when.date()).days
    clock = when.strftime("%H:%M")
    if days == 0:
        return f"today {clock}"
    if days == 1:
        return f"yesterday {clock}"
    day = when.strftime("%-d %b") + ("" if when.year == current.year else f" {when.year}")
    return f"{day} {clock}"


@dataclass(frozen=True)
class SessionUsage:
    """What a conversation (all its runs, subagents included) has consumed so far.

    ``cost_usd`` sums only the calls the provider priced; ``priced_calls < calls`` means some
    were unpriced (a local model, a provider that reports no cost) and the total is a lower bound.
    """

    calls: int = 0
    priced_calls: int = 0
    input_tokens: int = 0
    #: Input tokens of the most recent call: the size of the context the model last saw.
    last_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    #: Input tokens served from the provider's prompt cache (a part of ``input_tokens``).
    cache_read_tokens: int = 0
    #: Input tokens written to the prompt cache (Anthropic-style caches charge for writes).
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    #: Priced spend per model name, so a split between a main and a small model is visible.
    by_model: Mapping[str, float] = field(default_factory=dict)

    def plus(self, data: Mapping[str, Any]) -> SessionUsage:
        """This usage plus one ``model.usage`` event's data (for live running totals)."""
        cost = data.get("cost_usd")
        priced = isinstance(cost, (int, float))
        return SessionUsage(
            calls=self.calls + 1,
            priced_calls=self.priced_calls + (1 if priced else 0),
            input_tokens=self.input_tokens + int(data.get("input_tokens", 0) or 0),
            last_input_tokens=int(data.get("input_tokens", 0) or 0),
            output_tokens=self.output_tokens + int(data.get("output_tokens", 0) or 0),
            reasoning_tokens=self.reasoning_tokens + int(data.get("reasoning_tokens", 0) or 0),
            cache_read_tokens=self.cache_read_tokens + int(data.get("cache_read_tokens", 0) or 0),
            cache_write_tokens=self.cache_write_tokens
            + int(data.get("cache_write_tokens", 0) or 0),
            cost_usd=self.cost_usd + (float(cost) if isinstance(cost, (int, float)) else 0.0),
            by_model=_add_model_cost(self.by_model, data.get("model"), cost),
        )


def _add_model_cost(
    so_far: Mapping[str, float], model: object, cost: object
) -> Mapping[str, float]:
    if not isinstance(cost, (int, float)):
        return so_far
    name = model if isinstance(model, str) and model else "unknown"
    return {**so_far, name: so_far.get(name, 0.0) + float(cost)}


def model_breakdown(usage: SessionUsage) -> str:
    """``glm-5.3-flash $0.0004 · deepseek-v4-pro $0.012``, biggest first; ``""`` for one model."""
    if len(usage.by_model) < 2:
        return ""
    ranked = sorted(usage.by_model.items(), key=lambda item: -item[1])
    return " · ".join(f"{name.rsplit('/', 1)[-1]} {format_cost(cost)}" for name, cost in ranked)


def format_cost(usd: float) -> str:
    """A dollar amount with enough precision for cheap models: ``$0.0042``, ``$1.23``."""
    if usd == 0:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.4f}"
    return f"${usd:.2f}" if usd >= 1 else f"${usd:.3f}"


def format_tokens(count: int) -> str:
    """``950``, ``12.3k``, ``1.2M``."""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k".replace(".0k", "k")
    return f"{count / 1_000_000:.1f}M".replace(".0M", "M")


def usage_summary(usage: SessionUsage, *, context: bool = True) -> str:
    """One line every gateway shows: cost, tokens, calls; flags unpriced calls honestly.

    ``context=False`` leaves out the context size, for a gateway that shows it elsewhere.
    """
    if usage.calls == 0:
        return "no model calls yet"
    cost = format_cost(usage.cost_usd) if usage.priced_calls else "cost unknown"
    if 0 < usage.priced_calls < usage.calls:
        cost += f" (+{usage.calls - usage.priced_calls} unpriced)"
    return (
        f"{cost} · {format_tokens(usage.input_tokens)} in"
        + (f" ({format_tokens(usage.cache_read_tokens)} cached)" if usage.cache_read_tokens else "")
        + " · "
        f"{format_tokens(usage.output_tokens)} out · {usage.calls} calls"
        + (
            f" · context {format_tokens(usage.last_input_tokens)}"
            if usage.last_input_tokens and context
            else ""
        )
    )


@dataclass(frozen=True)
class HistoryItem:
    """One row of a stored conversation, in a gateway-neutral shape.

    ``kind`` is ``user``, ``assistant``, ``tool`` or ``compaction`` (the summary that replaced
    the messages before it); ``text`` is the message text, the summary or the tool name, ``args`` carries a tool call's arguments and ``result`` its (already scrubbed)
    output, when the call has been answered.
    """

    kind: str
    text: str
    args: Mapping[str, Any] | None = None
    result: str | None = None
    #: A user message's images: stored file names (``Application.session_image`` serves them).
    images: tuple[str, ...] = ()
    #: The run that wrote it ("" for lines written before P54 or by a store without run ids).
    run_id: str = ""


#: A prompt token that may name a file: ``@path``, a quoted path, or one with ``\ `` escapes.
_PATH_TOKEN_RE = re.compile(r"""(?<!\S)(@?)(?:'([^']+)'|"([^"]+)"|((?:\\ |\S)+))""")


def image_paths(text: str, base: Path | None = None) -> tuple[str, list[Path]]:
    """The image files a prompt attaches, and the prompt without them (P44).

    ``@path/to/img.png`` attaches a file relative to ``base`` (the working directory); a pasted
    or dragged absolute path (``/Users/me/My\\ Shot.png``, ``'~/a b.png'``) does too. Only
    existing PNG, JPEG, WebP or GIF files count; any other token stays in the text.
    """
    found: list[Path] = []

    def take(match: re.Match[str]) -> str:
        at, *raw = match.groups()
        name = next(g for g in raw if g is not None).replace("\\ ", " ")
        if not name.lower().endswith(IMAGE_SUFFIXES) or not (at or name.startswith(("/", "~"))):
            return match.group(0)
        path = Path(name).expanduser()
        if not path.is_absolute():
            path = (base or Path.cwd()) / path
        if not path.is_file():
            return match.group(0)
        found.append(path)
        return ""

    rest = _PATH_TOKEN_RE.sub(take, text)
    return (re.sub(r"[ \t]{2,}", " ", rest).strip() if found else text), found


def load_images(paths: Sequence[Path]) -> tuple[Image, ...]:
    """Read and check the attached files; ValueError names the first one that is not usable."""
    return tuple(load_image(path) for path in paths)


@runtime_checkable
class Gateway(Protocol):
    """A gateway drives the Application and consumes AgentEvents.

    Implementations convert their native protocol into MessageEnvelopes and
    render the returned event stream. A gateway never touches tools, models, or
    stores directly — it imports the Application and this contract only.
    """

    async def handle_message(
        self,
        application: Application,
        envelope: MessageEnvelope,
    ) -> AsyncIterator[AgentEvent]:
        """Dispatch one message through the Application and stream its events."""
        ...
