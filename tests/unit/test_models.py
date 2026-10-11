"""Unit tests for model capabilities (network-free)."""

from __future__ import annotations

import dataclasses

import pytest

from agent_stack.core.errors import ConfigurationError
from agent_stack.models.capabilities import ModelCapabilities, require_tool_calling
from agent_stack.models.factory import build_model


def test_require_tool_calling_passes_when_supported() -> None:
    require_tool_calling(ModelCapabilities(tool_calling=True), model="m")


def test_require_tool_calling_raises_when_unsupported() -> None:
    with pytest.raises(ConfigurationError):
        require_tool_calling(ModelCapabilities(tool_calling=False), model="m")


def test_model_capabilities_is_frozen() -> None:
    caps = ModelCapabilities(tool_calling=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.tool_calling = False


def test_build_model_unknown_provider_raises_configuration_error() -> None:
    # An unknown provider prefix fails during resolution; no network or API key needed.
    with pytest.raises(ConfigurationError):
        build_model("nope:does-not-exist")
