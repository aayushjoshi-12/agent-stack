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
