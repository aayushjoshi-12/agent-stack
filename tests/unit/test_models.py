"""Unit tests for model construction (network-free)."""

from __future__ import annotations

import pytest

from agent_stack.core.errors import ConfigurationError
from agent_stack.models.factory import build_model


def test_build_model_unknown_provider_raises_configuration_error() -> None:
    # An unknown provider prefix fails during resolution; no network or API key needed.
    with pytest.raises(ConfigurationError):
        build_model("nope:does-not-exist")
