"""Unit tests for `AppConfig.from_env`."""

from __future__ import annotations

from pathlib import Path

from agent_stack.app.config import AppConfig


def test_from_env_empty_matches_defaults() -> None:
    assert AppConfig.from_env({}) == AppConfig()


def test_workspace_defaults_to_current_directory() -> None:
    assert AppConfig().workspace == Path.cwd()


def test_database_defaults_into_the_workspace() -> None:
    assert AppConfig().database == Path.cwd() / "agent.db"
    assert AppConfig(workspace=Path("/tmp/ws")).database == Path("/tmp/ws/agent.db")


def test_database_respects_an_explicit_override() -> None:
    cfg = AppConfig(workspace=Path("/tmp/ws"), db_path=Path("/tmp/ws/index.db"))
    assert cfg.database == Path("/tmp/ws/index.db")


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
