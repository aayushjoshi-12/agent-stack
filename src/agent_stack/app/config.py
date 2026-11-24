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

#: The small model used when the main model is on OpenRouter and none is chosen: the cheapest
#: tool-capable model that completed real agent tasks in testing (see docs/STATUS.md).
DEFAULT_SMALL_MODEL = "openrouter:z-ai/glm-5.3-flash"
_NO_SPLIT = frozenset({"", "main", "same", "none", "off"})


def _small_model(raw: str | None, main_model: str) -> str | None:
    """Resolve ``AGENT_STACK_SMALL_MODEL``: unset picks a default only where one is known to work.

    A default for a provider the user may not have a key for would break their setup, so outside
    OpenRouter an unset value means "use the main model for everything".
    """
    if raw is None:
        return DEFAULT_SMALL_MODEL if main_model.startswith("openrouter:") else None
    value = raw.strip()
    if value.lower() in _NO_SPLIT or value == main_model:
        return None
    return value


def _default_home() -> Path:
    raw = os.environ.get("AGENT_STACK_HOME")
    return Path(raw).expanduser() if raw else Path.home() / ".agent-stack"


@dataclass(frozen=True)
class AppConfig:
    """Immutable application configuration.

    `workspace` defaults to the **current directory**, so running `agent-stack` inside a
    project opens that project. The SQLite index lives at `<workspace>/agent.db` unless
    `AGENT_STACK_DB` (``db_path``) overrides it.
    """

    model: str = "openrouter:deepseek/deepseek-v4-pro"
    #: A cheaper model for side work that does not need the main model's judgement: the
    #: read-only ``explorer`` subagent and conversation titles (the same split Claude Code makes
    #: with its Haiku-class model). Code edits and answers always use ``model``. ``None`` means
    #: "no split": everything uses ``model``.
    small_model: str | None = None
    #: Context window (tokens) of ``model``; only used for the TUI's usage meter.
    context_window: int = 1_000_000
    #: Tokens the model is shown at most (default 120k, far below ``context_window``): past it,
    #: old tool results are cleared and then the oldest messages dropped. Long contexts cost
    #: more and make models worse at using what is in them. ``AGENT_STACK_CONTEXT_BUDGET``.
    context_budget: int = 120_000
    #: Model calls one run may make (tool calls are capped at twice this). The default suits
    #: chat; a long autonomous task needs more. ``AGENT_STACK_MAX_STEPS``.
    max_steps: int = 50
    #: Per-conversation spend cap in USD; a conversation at or over it starts no further run.
    #: ``None`` (the default) means no cap.
    budget_usd: float | None = None
    #: Tools that pause for a human's approval before they run. Comma-separated in
    #: ``AGENT_STACK_APPROVAL_TOOLS``; set it empty to run everything unattended.
    approval_tools: tuple[str, ...] = ("run_command", "write_file", "edit_file")
    workspace: Path = field(default_factory=Path.cwd)
    #: Where state shared by every project lives (global instructions, ideas, cross-project
    #: memory). ``AGENT_STACK_HOME``; defaults to ``~/.agent-stack``.
    home: Path = field(default_factory=lambda: _default_home())
    #: Extra skill folders (e.g. ``~/.claude/skills``), colon-separated in
    #: ``AGENT_STACK_SKILLS_PATH``. Nothing outside the workspace is read unless listed here.
    skill_roots: tuple[Path, ...] = ()
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

        home_raw = source.get("AGENT_STACK_HOME")
        skills_raw = source.get("AGENT_STACK_SKILLS_PATH", "")
        skill_roots = tuple(
            Path(p.strip()).expanduser() for p in skills_raw.split(":") if p.strip()
        )

        model = source.get("AGENT_STACK_MODEL", defaults.model)
        return cls(
            model=model,
            small_model=_small_model(source.get("AGENT_STACK_SMALL_MODEL"), model),
            approval_tools=approval_tools,
            context_window=int(source.get("AGENT_STACK_CONTEXT_WINDOW", defaults.context_window)),
            context_budget=int(source.get("AGENT_STACK_CONTEXT_BUDGET", defaults.context_budget)),
            max_steps=int(source.get("AGENT_STACK_MAX_STEPS", defaults.max_steps)),
            budget_usd=budget,
            workspace=workspace,
            home=Path(home_raw).expanduser() if home_raw else _default_home(),
            skill_roots=skill_roots,
            db_path=db_path,
            theme=source.get("AGENT_STACK_THEME", defaults.theme),
        )
