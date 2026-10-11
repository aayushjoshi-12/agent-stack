"""Typed, frozen application configuration.

This is the *only* module that reads `os.environ`; everything else receives an
`AppConfig` from the composition root.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from agent_stack.core.errors import ConfigurationError


@dataclass(frozen=True)
class AppConfig:
    """Immutable application configuration."""

    model: str = "openrouter:z-ai/glm-5.3"
    workspace: Path = Path("./workspace")
    db_path: Path = Path("./workspace/agent.db")
    max_steps: int = 50
    max_tool_calls: int = 100
    max_concurrent_tools: int = 8

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AppConfig:
        """Build a config from environment variables, falling back to defaults.

        When `env` is None, `os.environ` is used. Paths expand a leading `~`.
        """
        source: Mapping[str, str] = os.environ if env is None else env
        defaults = cls()

        workspace_raw = source.get("AGENT_STACK_WORKSPACE")
        workspace = (
            defaults.workspace if workspace_raw is None else Path(workspace_raw).expanduser()
        )

        db_raw = source.get("AGENT_STACK_DB")
        db_path = defaults.db_path if db_raw is None else Path(db_raw).expanduser()

        return cls(
            model=source.get("AGENT_STACK_MODEL", defaults.model),
            workspace=workspace,
            db_path=db_path,
            max_steps=_read_int(source, "AGENT_STACK_MAX_STEPS", defaults.max_steps),
            max_tool_calls=_read_int(source, "AGENT_STACK_MAX_TOOL_CALLS", defaults.max_tool_calls),
            max_concurrent_tools=_read_int(
                source, "AGENT_STACK_MAX_CONCURRENT_TOOLS", defaults.max_concurrent_tools
            ),
        )


def _read_int(source: Mapping[str, str], name: str, default: int) -> int:
    """Return `source[name]` as an int, or `default` when the var is absent."""
    raw = source.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc
