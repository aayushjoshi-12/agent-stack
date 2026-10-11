"""Unit tests for `AppConfig.from_env`."""

from __future__ import annotations

from pathlib import Path

from agent_stack.app.config import AppConfig


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
