"""The single place that turns a model string into a `BaseChatModel`.

Provider-specific construction must not leak into the runtime: everything outside
`models/` deals only with the returned `BaseChatModel`.
"""

from __future__ import annotations

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from agent_stack.core.errors import ConfigurationError


def build_model(model: str, *, temperature: float = 0.0) -> BaseChatModel:
    """Build a chat model from a `provider:model` string.

    Construction failures (unknown provider, missing credentials, bad model id) are
    wrapped in :class:`ConfigurationError` so callers get one error type.
    """
    try:
        return init_chat_model(model, temperature=temperature)
    except Exception as exc:
        raise ConfigurationError(f"Failed to build model {model!r}: {exc}") from exc
