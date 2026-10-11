"""The agent-stack error hierarchy.

Every error raised by the runtime derives from :class:`AgentStackError` so callers can
distinguish our failures from unrelated exceptions.
"""

from __future__ import annotations


class AgentStackError(Exception):
    """Base class for every error raised by agent-stack."""


class ConfigurationError(AgentStackError):
    """Raised when configuration is missing, malformed, or unsupported."""


class ToolError(AgentStackError):
    """Base class for tool-related failures."""


class UnknownToolError(ToolError):
    """Raised when a tool call names a tool the registry does not know."""


class LimitExceededError(AgentStackError):
    """Base class for limits that terminate a run."""


class StepLimitExceeded(LimitExceededError):
    """Raised when the loop exceeds its maximum number of steps."""


class ToolCallLimitExceeded(LimitExceededError):
    """Raised when a run exceeds its maximum number of tool calls."""


class TimeoutExceeded(LimitExceededError):
    """Raised when a run exceeds its maximum wall-clock runtime."""


class SessionNotFoundError(AgentStackError):
    """Raised when a session id cannot be resolved."""


class ModelError(AgentStackError):
    """Raised when the model call itself fails (transport, provider error)."""


class ToolTimeoutError(ToolError):
    """Raised when a tool exceeds its individual time budget."""


class WorkspaceEscapeError(ToolError):
    """Raised when a path resolves outside the configured workspace."""


class CommandTimeoutError(ToolError):
    """Raised when a terminal command exceeds its time budget."""


class PersistenceError(AgentStackError):
    """Base class for store/persistence failures."""


class CheckpointError(PersistenceError):
    """Raised when a checkpoint cannot be written or read."""


class ArtifactError(AgentStackError):
    """Raised when an artifact cannot be stored or retrieved."""


class RunCancelled(AgentStackError):
    """Raised when a run is cancelled cooperatively."""


class RunNotFoundError(AgentStackError):
    """Raised when a run id cannot be resolved."""


class SessionVersionConflictError(PersistenceError):
    """Raised when saving a session whose version no longer matches the stored one."""


class SkillsError(AgentStackError):
    """Raised when a skill cannot be discovered, parsed, or loaded."""


class InstructionsError(AgentStackError):
    """Raised when agent instruction files cannot be loaded."""
