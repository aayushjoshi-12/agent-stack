"""Unit tests for the skills package (P16b): ``SKILL.md`` discovery + progressive loading.

Fixtures live in ``tests/skills_data/``. Root layout:

* ``skills-root/``       — valid skills (``good-skill``, ``with-resources``, ``zeta-skill``)
                          plus a ``notes/`` directory that is *not* a skill.
* ``more-skills-root/``  — ``alpha-skill``, ``beta-skill`` (merged ordering across roots).
* ``bad-name-root/``     — frontmatter name fails the charset rule.
* ``dir-mismatch-root/`` — frontmatter name differs from the directory name.
* ``nodesc-root/``       — missing ``description``.
* ``multiline-root/``    — block-scalar (``|``) description, unsupported.
* ``duplicate-root/``    — a skill reusing ``good-skill``'s name.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from langchain_core.tools import BaseTool

from agent_stack.core.errors import SkillsError
from agent_stack.skills import Skill, SkillLoader, SkillRegistry, SkillResource, skill_tools

SKILLS_DATA = Path(__file__).resolve().parent.parent / "skills_data"


def root(name: str) -> Path:
    return SKILLS_DATA / name


def test_discovers_skill_metadata() -> None:
    loader = SkillLoader([root("skills-root"), root("more-skills-root")])
    discovered = loader.discover()

    # Sorted across roots, and the SKILL.md-less `notes/` directory is ignored.
    assert [item.name for item in discovered] == [
        "alpha-skill",
        "beta-skill",
        "good-skill",
        "with-resources",
        "zeta-skill",
    ]

    good = next(item for item in discovered if item.name == "good-skill")
    assert good.description == "A valid demonstration skill whose answer lives only in its body."
    assert good.path == (root("skills-root") / "good-skill").resolve()
    assert good.allowed_tools == ()

    with_resources = next(item for item in discovered if item.name == "with-resources")
    assert with_resources.allowed_tools == ("read_skill_resource", "terminal")


@pytest.mark.parametrize("bad_root", ["bad-name-root", "dir-mismatch-root"])
def test_rejects_invalid_skill_name(bad_root: str) -> None:
    loader = SkillLoader([root(bad_root)])

    with pytest.raises(SkillsError):
        loader.discover()


def test_rejects_missing_description() -> None:
    loader = SkillLoader([root("nodesc-root")])

    with pytest.raises(SkillsError, match="description"):
        loader.discover()


def test_loader_returns_body_not_metadata() -> None:
    loader = SkillLoader([root("skills-root")])
    skill = loader.load("good-skill")

    assert isinstance(skill, Skill)
    assert skill.metadata.name == "good-skill"
    assert skill.body == (
        "# Good Skill\n\n"
        "A skill that knows the answer to the question of life, "
        "but only after being loaded.\n\n"
        "The magic answer is 42."
    )
    # Level 2 is the body, never the frontmatter.
    assert "name: good-skill" not in skill.body
    assert "valid demonstration" not in skill.body
    assert not skill.body.endswith("\n")

    with pytest.raises(SkillsError, match="Unknown skill"):
        loader.load("no-such-skill")


def test_read_resource_reads_only_within_skill_dir() -> None:
    loader = SkillLoader([root("skills-root")])

    resource = loader.read_resource("with-resources", "references/ref.md")
    assert isinstance(resource, SkillResource)
    assert resource.name == "with-resources"
    assert resource.path.is_relative_to(root("skills-root") / "with-resources")
    assert (
        resource.path.read_text(encoding="utf-8")
        == "# Reference\n\nThe resource payload is: resource-content-7.\n"
    )

    for escaping_path in ("../SKILL.md", "../../etc/passwd", "/etc/passwd"):
        with pytest.raises(SkillsError):
            loader.read_resource("with-resources", escaping_path)

    with pytest.raises(SkillsError):
        loader.read_resource("with-resources", "references/missing.md")
    with pytest.raises(SkillsError):
        loader.read_resource("no-such-skill", "references/ref.md")


def test_system_prompt_lists_skill_metadata_only() -> None:
    registry = SkillRegistry(SkillLoader([root("skills-root"), root("more-skills-root")]))

    prompt = registry.metadata_for_system_prompt()
    lines = prompt.splitlines()

    assert len(lines) == 5
    assert all(re.fullmatch(r"^[a-z0-9-]+: .+$", line) for line in lines)
    assert lines[0].startswith("alpha-skill: ")
    assert lines[-1].startswith("zeta-skill: ")

    # Level 1 only: no body may leak into the resident block (token-budget assertion).
    assert "magic answer" not in prompt
    assert "resource-content-7" not in prompt


def test_duplicate_skill_names_across_roots_raise() -> None:
    loader = SkillLoader([root("skills-root"), root("duplicate-root")])

    with pytest.raises(SkillsError, match=r"[Dd]uplicate"):
        loader.discover()


def test_block_scalar_description_is_collapsed_to_one_line() -> None:
    (skill,) = SkillLoader([root("multiline-root")]).discover()

    assert skill.description == "A block scalar is folded into one line. Extra keys are ignored."


def test_malformed_skill_is_skipped_and_logged_when_not_strict(
    caplog: pytest.LogCaptureFixture,
) -> None:
    loader = SkillLoader([root("nodesc-root"), root("skills-root")], strict=False)

    names = [m.name for m in loader.discover()]

    assert "nodesc" not in names and "good-skill" in names
    assert "skipping skill" in caplog.text and "nodesc" in caplog.text


def test_registry_get_and_metadata_are_deterministic() -> None:
    registry = SkillRegistry(SkillLoader([root("skills-root"), root("more-skills-root")]))

    names = [item.name for item in registry.metadata()]
    assert names == sorted(names)
    assert registry.get("good-skill").name == "good-skill"
    with pytest.raises(SkillsError, match="Unknown skill"):
        registry.get("no-such-skill")


def test_skill_tools_are_named_and_bound() -> None:
    registry = SkillRegistry(SkillLoader([root("skills-root")]))
    tools = skill_tools(registry)

    assert len(tools) == 2
    assert all(isinstance(tool, BaseTool) for tool in tools)
    assert [tool.name for tool in tools] == ["load_skill", "read_skill_resource"]


async def test_load_skill_tool_returns_body() -> None:
    registry = SkillRegistry(SkillLoader([root("skills-root")]))
    load_skill, _ = skill_tools(registry)

    body = await load_skill.ainvoke({"name": "good-skill"})
    assert "The magic answer is 42." in body


async def test_read_skill_resource_tool_returns_contents() -> None:
    registry = SkillRegistry(SkillLoader([root("skills-root")]))
    _, read_skill_resource = skill_tools(registry)

    content = await read_skill_resource.ainvoke(
        {"name": "with-resources", "path": "references/ref.md"}
    )
    assert "resource-content-7" in content


async def test_skill_tools_return_error_strings_not_raise() -> None:
    registry = SkillRegistry(SkillLoader([root("skills-root")]))
    load_skill, read_skill_resource = skill_tools(registry)

    assert (await load_skill.ainvoke({"name": "no-such-skill"})).startswith("Error:")
    assert (
        await read_skill_resource.ainvoke({"name": "with-resources", "path": "../SKILL.md"})
    ).startswith("Error:")
