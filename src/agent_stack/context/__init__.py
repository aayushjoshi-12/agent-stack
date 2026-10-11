"""Agent instructions: ``AGENTS.md`` / ``CLAUDE.md`` resolution for context assembly.

Definitions only (Phase 16c): the loader is stateless beyond its size-cap cache and
produces immutable :class:`InstructionFile` data consumed by the
:class:`~agent_stack.core.context.ContextBuilder`.
"""

from __future__ import annotations

from agent_stack.context.instructions import InstructionFile, InstructionsLoader

__all__ = ["InstructionFile", "InstructionsLoader"]