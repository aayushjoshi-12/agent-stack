"""File-based skills: ``SKILL.md`` discovery and progressive loading (P16b).

A skill is a directory containing a ``SKILL.md`` whose frontmatter declares ``name`` and
``description``. Only that metadata is resident in the system prompt; the instruction
body and resource files are loaded on demand through the ``load_skill`` and
``read_skill_resource`` tools.
"""

from agent_stack.skills.loader import SkillLoader
from agent_stack.skills.models import Skill, SkillMetadata, SkillResource
from agent_stack.skills.registry import SkillRegistry
from agent_stack.skills.tools import skill_tools

__all__ = [
    "Skill",
    "SkillLoader",
    "SkillMetadata",
    "SkillRegistry",
    "SkillResource",
    "skill_tools",
]