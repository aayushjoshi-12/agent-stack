"""Compose the ordered resident system-prompt parts for a run (Phase 16e).

:class:`SystemPromptComposer` implements core's ``SystemPartsProvider``: it turns the
file-based capability loaders into the ordered blocks the runtime puts in front of the
conversation — base prompt, then project instructions (``AGENTS.md``), then skill
metadata, then the ``MEMORY.md`` index (P16d). Instructions resolve from the confined
workspace root, not the process CWD, so what a run sees is a function of its workspace.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from agent_stack.context.instructions import InstructionFile, InstructionsLoader
from agent_stack.memory import MemoryIndex
from agent_stack.skills import SkillRegistry

__all__ = ["SystemPromptComposer"]

#: Short headers keep each resident block legible and independently droppable by the cap.
_INSTRUCTIONS_HEADER = "# Project instructions (AGENTS.md)"
_SKILLS_HEADER = "# Available skills"
_SKILLS_FOOTER = "Use the load_skill tool to read a skill's instructions before using it."
_MEMORY_HEADER = "# Memory"
_MEMORY_FOOTER = "Use the read_memory_topic tool to read a topic's notes before relying on it."


class SystemPromptComposer:
    """Builds the resident system-prompt parts for a workspace."""

    def __init__(
        self,
        *,
        instructions: InstructionsLoader,
        skills: SkillRegistry,
        memory: MemoryIndex,
        workspace_root: Path,
    ) -> None:
        self._instructions = instructions
        self._skills = skills
        self._memory = memory
        self._workspace_root = workspace_root

    async def parts(self, *, base_prompt: str) -> Sequence[str]:
        """Ordered resident parts: base prompt, instructions, skills metadata, memory index."""
        parts: list[str] = [base_prompt]

        files = await self._instructions.load(self._workspace_root)
        if files:
            parts.append(_instructions_block(files))

        listing = self._skills.metadata_for_system_prompt()
        if listing:
            parts.append(_skills_block(listing))

        index = self._memory.index()
        if index:
            parts.append(_memory_block(index))

        return tuple(parts)


def _instructions_block(files: Sequence[InstructionFile]) -> str:
    """Delimit the resolved instructions files (nearest-first) under one header."""
    bodies = "\n\n".join(file.content for file in files)
    return f"{_INSTRUCTIONS_HEADER}\n\n{bodies}"


def _skills_block(listing: str) -> str:
    """Delimit the level-1 skill metadata block (names + descriptions only)."""
    return f"{_SKILLS_HEADER}\n\n{listing}\n\n{_SKILLS_FOOTER}"


def _memory_block(index: str) -> str:
    """Delimit the level-1 memory index block (topic lines only, no bodies)."""
    return f"{_MEMORY_HEADER}\n\n{index}\n\n{_MEMORY_FOOTER}"
