"""Unit tests for `AppConfig.from_env`."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_stack.app.config import AppConfig
from agent_stack.core.errors import ConfigurationError


def test_from_env_empty_matches_defaults() -> None:
    assert AppConfig.from_env({}) == AppConfig()


def test_from_env_overrides_model() -> None:
    cfg = AppConfig.from_env({"AGENT_STACK_MODEL": "openai:gpt-4o"})
    assert cfg.model == "openai:gpt-4o"


def test_from_env_overrides_paths() -> None:
    cfg = AppConfig.from_env(
        {
            "AGENT_STACK_WORKSPACE": "/tmp/ws",
            "AGENT_STACK_DB": "/tmp/ws/agent.db",
        }
    )
    assert cfg.workspace == Path("/tmp/ws")
    assert cfg.db_path == Path("/tmp/ws/agent.db")


def test_from_env_overrides_ints() -> None:
    cfg = AppConfig.from_env(
        {
            "AGENT_STACK_MAX_STEPS": "7",
            "AGENT_STACK_MAX_TOOL_CALLS": "9",
            "AGENT_STACK_MAX_CONCURRENT_TOOLS": "3",
        }
    )
    assert cfg.max_steps == 7
    assert cfg.max_tool_calls == 9
    assert cfg.max_concurrent_tools == 3


@pytest.mark.parametrize(
    "var",
    [
        "AGENT_STACK_MAX_STEPS",
        "AGENT_STACK_MAX_TOOL_CALLS",
        "AGENT_STACK_MAX_CONCURRENT_TOOLS",
    ],
)
def test_from_env_invalid_int_raises(var: str) -> None:
    with pytest.raises(ConfigurationError):
        AppConfig.from_env({var: "not-an-int"})


def test_from_env_expands_user() -> None:
    cfg = AppConfig.from_env(
        {
            "AGENT_STACK_WORKSPACE": "~/agent-stack-ws",
            "AGENT_STACK_DB": "~/agent-stack-ws/agent.db",
        }
    )
    assert "~" not in str(cfg.workspace)
    assert "~" not in str(cfg.db_path)
    assert cfg.workspace == Path.home() / "agent-stack-ws"
    assert cfg.db_path == Path.home() / "agent-stack-ws" / "agent.db"
