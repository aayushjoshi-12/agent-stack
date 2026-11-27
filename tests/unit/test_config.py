"""Unit tests for `AppConfig.from_env`."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_stack.app.config import AppConfig


def test_from_env_empty_matches_defaults() -> None:
    # The one resolved default: with the default (OpenRouter) main model, the environment-built
    # config also gets the known-good small model. A hand-built AppConfig() never picks a provider.
    from agent_stack.app.config import DEFAULT_SMALL_MODEL

    assert AppConfig.from_env({}) == AppConfig(small_model=DEFAULT_SMALL_MODEL)
    assert AppConfig().small_model is None


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


def test_default_model_is_deepseek_v4_pro_on_openrouter() -> None:
    assert AppConfig().model == "openrouter:deepseek/deepseek-v4-pro"


def test_context_window_defaults_and_is_overridable() -> None:
    assert AppConfig().context_window == 1_000_000
    assert AppConfig.from_env({"AGENT_STACK_CONTEXT_WINDOW": "128000"}).context_window == 128_000


def test_home_and_skill_roots_come_from_env() -> None:
    config = AppConfig.from_env(
        {"AGENT_STACK_HOME": "/tmp/h", "AGENT_STACK_SKILLS_PATH": "/a/skills: /b/skills:"}
    )

    assert config.home == Path("/tmp/h")
    assert config.skill_roots == (Path("/a/skills"), Path("/b/skills"))


def test_max_runtime_is_configurable() -> None:
    assert AppConfig.from_env({}).max_runtime_seconds == 1800.0
    assert AppConfig.from_env({"AGENT_STACK_MAX_RUNTIME": "7200"}).max_runtime_seconds == 7200.0


def test_fetch_mode_defaults_to_reader_and_is_validated() -> None:
    assert AppConfig.from_env({}).fetch_mode == "reader"
    assert AppConfig.from_env({"AGENT_STACK_FETCH_MODE": "Direct"}).fetch_mode == "direct"
    from agent_stack.core.errors import ConfigurationError

    with pytest.raises(ConfigurationError):
        AppConfig.from_env({"AGENT_STACK_FETCH_MODE": "proxy"})


def test_compact_at_takes_a_share_or_off() -> None:
    assert AppConfig.from_env({}).compact_at == 0.8
    assert AppConfig.from_env({"AGENT_STACK_COMPACT_AT": "0.6"}).compact_at == 0.6
    assert AppConfig.from_env({"AGENT_STACK_COMPACT_AT": "off"}).compact_at is None
    for bad in ("1.5", "-1", "soon"):
        with pytest.raises(Exception, match="COMPACT_AT"):
            AppConfig.from_env({"AGENT_STACK_COMPACT_AT": bad})
