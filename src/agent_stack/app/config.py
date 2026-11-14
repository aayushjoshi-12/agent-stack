"""Typed, frozen application configuration.

This is the *only* module that reads `os.environ`; everything else receives an
`AppConfig` from the composition root.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agent_stack.core.errors import ConfigurationError


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
    #: Per-conversation spend cap in USD; a conversation at or over it starts no further run.
    #: ``None`` (the default) means no cap.
    budget_usd: float | None = None
    #: Tools that pause for a human's approval before they run. Comma-separated in
    #: ``AGENT_STACK_APPROVAL_TOOLS``; set it empty to run everything unattended.
    approval_tools: tuple[str, ...] = ("run_command", "write_file", "edit_file")
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

        approval_raw = source.get("AGENT_STACK_APPROVAL_TOOLS")
        approval_tools = (
            defaults.approval_tools
            if approval_raw is None
            else tuple(name.strip() for name in approval_raw.split(",") if name.strip())
        )

        budget_raw = source.get("AGENT_STACK_BUDGET_USD", "").strip()
        try:
            budget = float(budget_raw) if budget_raw else None
        except ValueError:
            raise ConfigurationError("AGENT_STACK_BUDGET_USD must be a number") from None
        if budget is not None and budget < 0:
            raise ConfigurationError("AGENT_STACK_BUDGET_USD must not be negative")

        return cls(
            model=source.get("AGENT_STACK_MODEL", defaults.model),
            approval_tools=approval_tools,
            context_window=int(source.get("AGENT_STACK_CONTEXT_WINDOW", defaults.context_window)),
            budget_usd=budget,
            workspace=workspace,
            db_path=db_path,
            theme=source.get("AGENT_STACK_THEME", defaults.theme),
        )
