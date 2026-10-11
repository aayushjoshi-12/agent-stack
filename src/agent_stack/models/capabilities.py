"""Model capability declarations and validation.

Capabilities are a definition, not a runtime probe: callers declare what a model
supports, and the runtime validates before relying on a feature.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_stack.core.errors import ConfigurationError


@dataclass(frozen=True)
class ModelCapabilities:
    """What a model can do, as declared by configuration."""

    tool_calling: bool = False
    streaming: bool = False
    structured_output: bool = False


def require_tool_calling(caps: ModelCapabilities, *, model: str) -> None:
    """Raise :class:`ConfigurationError` unless the model supports tool calling."""
    if not caps.tool_calling:
        raise ConfigurationError(f"Model {model!r} does not support tool calling.")
