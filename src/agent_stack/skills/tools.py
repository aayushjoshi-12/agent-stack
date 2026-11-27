"""Skill tools: ``load_skill`` (level 2) and ``read_skill_resource`` (level 3).

These implement progressive disclosure: the model sees only skill metadata in its system
prompt, then calls ``load_skill`` to read the instructions of a skill it wants to use,
and ``read_skill_resource`` to pull a specific file (``scripts/``, ``references/``,
``assets/``) on demand.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from agent_stack.core.errors import SkillsError
from agent_stack.core.run_context import current_run_context
from agent_stack.skills.registry import SkillRegistry

__all__ = ["skill_tools"]


def skill_tools(registry: SkillRegistry) -> tuple[BaseTool, ...]:
    """Build the progressive-disclosure tool set bound to ``registry``."""

    @tool
    async def load_skill(name: str) -> str:
        """Read the instructions of a named skill (its SKILL.md body).

        Skills are listed in the system prompt by name and description only; call this
        tool to load a skill's full body before using it.
        """
        try:
            skill = registry.load(name)
        except SkillsError as exc:
            return f"Error: {exc}"
        allowed = skill.metadata.allowed_tools
        ctx = current_run_context()
        if allowed is None or ctx is None:
            return skill.body
        # P49: `allowed-tools` only narrows the run (never skips an approval); loading more
        # skills can only narrow it further, so the two skill tools stay callable.
        ctx.skill_scope.narrow(name, (*allowed, "load_skill", "read_skill_resource"))
        tools = ", ".join(sorted(ctx.skill_scope.allowed or ())) or "(none)"
        return f"{skill.body}\n\n[While this skill is active, this run may call only: {tools}]"

    @tool
    async def read_skill_resource(name: str, path: str) -> str:
        """Read one resource file from inside a skill directory (scripts/, references/, assets/).

        ``path`` is relative to the skill directory and must stay inside it.
        """
        try:
            resource = registry.read_resource(name, path)
        except SkillsError as exc:
            return f"Error: {exc}"
        try:
            return resource.path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return f"Error: could not read {path}: {exc}"

    return (load_skill, read_skill_resource)
