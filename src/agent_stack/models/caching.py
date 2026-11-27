"""Prompt-cache breakpoints for the providers that need them (ADR 017).

Most providers cache a repeated prefix on their own (OpenAI, DeepSeek, Grok, Moonshot, Z.AI,
Gemini 2.5+ through OpenRouter). Anthropic Claude and Alibaba Qwen only cache up to a block
marked ``cache_control``, so for those the model input gets two breakpoints: the system message
(caching the tool schemas and system prompt before it) and the last message (a rolling
breakpoint: the next call finds this call's write within the provider's lookback window).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage
from langchain_core.runnables import Runnable, RunnableLambda

CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}

#: Model-string prefixes whose provider caches only at explicit breakpoints.
_EXPLICIT = ("anthropic:", "openrouter:anthropic/", "openrouter:qwen/")


def needs_breakpoints(model: str) -> bool:
    """Whether ``model`` (a ``provider:model`` string) caches only at ``cache_control`` marks."""
    return model.startswith(_EXPLICIT)


def add_cache_breakpoints(messages: Sequence[BaseMessage]) -> list[BaseMessage]:
    """The messages with ``cache_control`` on the system message and the last text message.

    Returns copies of the two marked messages; the given messages are never mutated. A message
    with no text to mark (an assistant turn that is only tool calls) is skipped for the one
    before it. At most two breakpoints, inside Anthropic's limit of four.
    """
    out = list(messages)
    system = bool(out) and isinstance(out[0], SystemMessage)
    if system and (marked := _marked(out[0])):
        out[0] = marked
    for index in range(len(out) - 1, 0 if system else -1, -1):
        if marked := _marked(out[index]):
            out[index] = marked
            break
    return out


def with_prompt_cache(model: Runnable[Any, Any], name: str) -> Runnable[Any, Any]:
    """``model`` with breakpoints added to its input when ``name``'s provider needs them."""
    if not needs_breakpoints(name):
        return model
    return RunnableLambda(add_cache_breakpoints) | model


def _marked(message: BaseMessage) -> BaseMessage | None:
    """A copy of ``message`` whose last text block carries ``cache_control``; None if no text."""
    content = message.content
    if isinstance(content, str):
        if not content:
            return None
        blocks: list[Any] = [{"type": "text", "text": content, "cache_control": CACHE_CONTROL}]
        return message.model_copy(update={"content": blocks})
    for index in range(len(content) - 1, -1, -1):
        block = content[index]
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            blocks = list(content)
            blocks[index] = {**block, "cache_control": CACHE_CONTROL}
            return message.model_copy(update={"content": blocks})
    return None
