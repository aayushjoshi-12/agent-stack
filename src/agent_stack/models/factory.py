"""The single place that turns a model string into a `BaseChatModel`.

Provider-specific construction must not leak into the runtime: everything outside
`models/` deals only with the returned `BaseChatModel`.
"""

from __future__ import annotations

from langchain.chat_models import init_chat_model
from langchain_core.language_models import BaseChatModel

from agent_stack.core.errors import ConfigurationError


def build_model(
    model: str, *, temperature: float = 0.0, client_retries: bool = True
) -> BaseChatModel:
    """Build a chat model from a `provider:model` string.

    ``client_retries=False`` turns the provider client's own retries off, for the agent runtime,
    which retries transient errors itself (P51) where the run's deadline, cancellation and
    events are. Construction failures (unknown provider, missing credentials, bad model id) are
    wrapped in :class:`ConfigurationError` so callers get one error type.
    """
    try:
        if client_retries:
            return init_chat_model(model, temperature=temperature)
        built = init_chat_model(model, temperature=temperature, max_retries=0)
    except Exception as exc:
        raise ConfigurationError(f"Failed to build model {model!r}: {exc}") from exc
    sdk = getattr(getattr(built, "client", None), "sdk_configuration", None)
    if sdk is not None and hasattr(sdk, "retry_config"):
        # OpenRouter's SDK: with no retry config from langchain (max_retries=0) it falls back to
        # retrying 5XX for up to an hour, unseen. "none" really turns it off.
        from openrouter.utils import BackoffStrategy, RetryConfig

        sdk.retry_config = RetryConfig("none", BackoffStrategy(500, 60000, 1.5, 0), False)
    return built
