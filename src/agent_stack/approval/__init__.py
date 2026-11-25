"""Auto mode: decide gated tool calls without bothering the user, and ask when unsure."""

from agent_stack.approval.auto import AutoApprover, external_target

__all__ = ["AutoApprover", "external_target"]
