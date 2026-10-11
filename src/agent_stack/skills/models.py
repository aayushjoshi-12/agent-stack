"""Definition objects for file-based skills (P16b).

Skills follow the ``SKILL.md`` format: a directory with a frontmatter-bearing
``SKILL.md`` plus optional ``scripts/``, ``references/`` and ``assets/`` resources.
These dataclasses are pure definitions — frozen, immutable, and free of runtime state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = ["Skill", "SkillMetadata", "SkillResource"]


@dataclass(frozen=True)
class SkillMetadata:
    """Level-1 metadata for one skill, parsed from ``SKILL.md`` frontmatter.

    Only this (small) surface is resident in the system prompt: ``name`` plus
    ``description``, roughly ~100 tokens per skill. The instruction body and resource
    files are loaded on demand (levels 2 and 3).
    """

    name: str
    description: str
    path: Path  # the skill directory (absolute)
    allowed_tools: tuple[str, ...] = ()

    def metadata_for_system_prompt(self) -> str:
        """The single resident line for this skill: ``<name>: <description>``."""
        return f"{self.name}: {self.description}"


@dataclass(frozen=True)
class Skill:
    """A skill's metadata plus its full instruction body (level 2)."""

    metadata: SkillMetadata
    body: str  # markdown body, everything after the frontmatter

    def metadata_for_system_prompt(self) -> str:
        """The level-1 resident line; the body stays out of the prompt until loaded."""
        return self.metadata.metadata_for_system_prompt()


@dataclass(frozen=True)
class SkillResource:
    """A resolved file inside a skill directory (level 3)."""

    name: str
    path: Path  # absolute; guaranteed to live within the skill directory