"""Typed, frozen application configuration.

This is the *only* module that reads `os.environ`; everything else receives an
`AppConfig` from the composition root.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    """Immutable application configuration.

    `workspace` defaults to the **current directory**, so running `agent-stack` inside a
    project opens that project. The SQLite index lives at `<workspace>/agent.db` unless
    `AGENT_STACK_DB` (``db_path``) overrides it.
    """

    model: str = "openrouter:deepseek/deepseek-v4-pro"
    #: Context window (tokens) of ``model``; only used for the TUI's usage meter.
    context_window: int = 1_000_000
    workspace: Path = field(default_factory=Path.cwd)
    #: Explicit index path; ``None`` means ``<workspace>/agent.db`` (see :attr:`database`).
    db_path: Path | None = None
    #: TUI theme name. Defaults to the app's true-colour ``agent-stack-dark``; set
    #: ``agent-stack-light``, ``ansi-dark``/``ansi-light`` (terminal-native colours) or any
    #: Textual theme. See ADR 012.
    theme: str = "agent-stack-dark"

    @property
    def database(self) -> Path:
        """Where the SQLite index lives: the override, else ``<workspace>/agent.db``."""
        return self.db_path if self.db_path is not None else self.workspace / "agent.db"

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
        db_path = None if db_raw is None else Path(db_raw).expanduser()

        return cls(
            model=source.get("AGENT_STACK_MODEL", defaults.model),
            context_window=int(source.get("AGENT_STACK_CONTEXT_WINDOW", defaults.context_window)),
            workspace=workspace,
            db_path=db_path,
            theme=source.get("AGENT_STACK_THEME", defaults.theme),
        )
