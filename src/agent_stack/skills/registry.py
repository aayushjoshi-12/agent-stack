"""The :class:`SkillRegistry` — sorted, deterministic facade over a :class:`SkillLoader`.

The registry is what the rest of the runtime talks to: it lists level-1 metadata (for
the system prompt), loads level-2 bodies, and serves level-3 resources.
"""

from __future__ import annotations

from agent_stack.core.errors import SkillsError
from agent_stack.skills.loader import SkillLoader
from agent_stack.skills.models import Skill, SkillMetadata, SkillResource

__all__ = ["SkillRegistry"]


class SkillRegistry:
    """Thin, sorted wrapper over a :class:`SkillLoader`."""

    def __init__(self, loader: SkillLoader) -> None:
        self._loader = loader

    def metadata(self) -> list[SkillMetadata]:
        """All discovered skill metadata, sorted by name for determinism."""
        return sorted(self._loader.discover(), key=lambda item: item.name)

    def metadata_for_system_prompt(self) -> str:
        """One ``<name>: <description>`` line per skill (level 1, resident).

        Bodies are deliberately absent: this is the ~100-token-per-skill block that tells
        the model what skills exist and that they must be loaded before use.
        """
        return "\n".join(item.metadata_for_system_prompt() for item in self.metadata())

    def get(self, name: str) -> SkillMetadata:
        """Return the metadata for ``name``; unknown names raise :class:`SkillsError`."""
        for metadata in self.metadata():
            if metadata.name == name:
                return metadata
        raise SkillsError(f"Unknown skill: {name!r}")

    def load(self, name: str) -> Skill:
        """Return the full skill (metadata + body) for ``name`` (level 2)."""
        return self._loader.load(name)

    def read_resource(self, name: str, rel_path: str) -> SkillResource:
        """Return a resolved file inside the skill directory (level 3)."""
        return self._loader.read_resource(name, rel_path)