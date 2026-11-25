"""The agent-stack error hierarchy.

Every error raised by the runtime derives from :class:`AgentStackError` so callers can
distinguish our failures from unrelated exceptions.
"""

from __future__ import annotations


def error_text(exc: BaseException, limit: int = 400) -> str:
    """``Name: first line of the message`` — what a person reads, not the exception's repr
    (a provider error's repr carries its headers, cookies and body)."""
    lines = str(exc).strip().splitlines()
    text = f"{type(exc).__name__}: {lines[0]}" if lines else type(exc).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


class AgentStackError(Exception):
    """Base class for every error raised by agent-stack."""


class ConfigurationError(AgentStackError):
    """Raised when configuration is missing, malformed, or unsupported."""


class ToolError(AgentStackError):
    """Base class for tool-related failures."""


class LimitExceededError(AgentStackError):
    """Base class for limits that terminate a run."""


class BudgetExceededError(LimitExceededError):
    """A conversation has spent its configured budget; no further run is started for it."""


class StepLimitExceeded(LimitExceededError):
    """Raised when the loop exceeds its maximum number of steps."""


class ToolCallLimitExceeded(LimitExceededError):
    """Raised when a run exceeds its maximum number of tool calls."""


class RuntimeDeadlineExceeded(LimitExceededError):
    """The run used up ``AgentLimits.max_runtime_seconds`` of wall-clock time."""


class ToolTimeoutError(ToolError):
    """A tool call exceeded ``AgentLimits.tool_timeout_seconds``."""


class ApprovalError(AgentStackError):
    """An approval decision was refused: unknown id, wrong binding, or already decided."""


class WorkspaceEscapeError(ToolError):
    """Raised when a path resolves outside the configured workspace."""


class CommandTimeoutError(ToolError):
    """Raised when a terminal command exceeds its time budget."""


class PersistenceError(AgentStackError):
    """Base class for store/persistence failures."""


class ArtifactError(AgentStackError):
    """Raised when an artifact cannot be stored or retrieved."""


class RunCancelled(AgentStackError):
    """Raised when a run is cancelled cooperatively."""


class SessionVersionConflictError(PersistenceError):
    """Raised when saving a session whose version no longer matches the stored one."""


class SkillsError(AgentStackError):
    """Raised when a skill cannot be discovered, parsed, or loaded."""


class InstructionsError(AgentStackError):
    """Raised when agent instruction files cannot be loaded."""
