"""Discover and parse ``SKILL.md`` files (P16b, level 1-3).

A skill is a directory containing a ``SKILL.md`` with YAML frontmatter followed by a
Markdown body:

* **Discovery** reads only the frontmatter into :class:`SkillMetadata` (level 1).
* **``load``** returns the full body (level 2).
* **``read_resource``** serves files from inside the skill directory (level 3).

The frontmatter is parsed as YAML. A skill that is malformed fails loudly in strict mode;
the non-strict runtime mode skips it and logs a warning naming the skill.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path

import yaml

from agent_stack.core.errors import SkillsError
from agent_stack.skills.models import Skill, SkillMetadata, SkillResource

__all__ = ["MAX_DESCRIPTION_CHARS", "SkillLoader"]

#: Upper bound for a skill description per the ``SKILL.md`` spec.
MAX_DESCRIPTION_CHARS = 1024

_log = logging.getLogger(__name__)
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n(.*?\n)?---[ \t]*(?:\n|\Z)", re.DOTALL)


def _parse_skill_file(text: str) -> tuple[dict[str, str], str]:
    """Parse ``SKILL.md`` frontmatter into ``(fields, body)``.

    The frontmatter is real YAML (block scalars, nested ``metadata`` and unknown keys are all
    legal in the wild), so a skill written for another harness loads here too. Only ``name``,
    ``description`` and ``allowed-tools`` are used; a folded or literal description is
    collapsed to one line.
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillsError("SKILL.md must start with a '---' frontmatter block and close it")
    try:
        data = yaml.safe_load(match.group(1) or "") or {}
    except yaml.YAMLError as exc:
        raise SkillsError(f"Malformed frontmatter: {exc}") from exc
    if not isinstance(data, dict):
        raise SkillsError("Frontmatter must be a mapping of 'key: value' pairs")
    fields = {
        key: " ".join(str(data[key]).split())
        for key in ("name", "description", "allowed-tools")
        if data.get(key) is not None
    }
    return fields, text[match.end() :]


def _parse_skill(directory: Path) -> SkillMetadata:
    """Parse and validate the ``SKILL.md`` in ``directory`` into metadata."""
    skill_file = directory / "SKILL.md"
    try:
        fields, _body = _parse_skill_file(skill_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SkillsError(f"Could not read {skill_file}: {exc}") from exc

    name = fields.get("name", "")
    if not name:
        raise SkillsError(f"{directory}: missing required frontmatter field 'name'")
    if _NAME_RE.fullmatch(name) is None:
        raise SkillsError(
            f"{directory}: invalid skill name {name!r}; must match ^[a-z0-9][a-z0-9-]{{0,63}}$"
        )
    if name != directory.name:
        raise SkillsError(
            f"{directory}: frontmatter name {name!r} must equal the skill directory "
            f"name {directory.name!r}"
        )

    description = fields.get("description", "")
    if not description:
        raise SkillsError(f"{directory}: missing required frontmatter field 'description'")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise SkillsError(
            f"{directory}: description too long ({len(description)} chars; "
            f"max {MAX_DESCRIPTION_CHARS})"
        )

    allowed_tools = tuple(fields["allowed-tools"].split()) if "allowed-tools" in fields else ()
    return SkillMetadata(
        name=name,
        description=description,
        path=directory.resolve(),
        allowed_tools=allowed_tools,
    )


class SkillLoader:
    """Discovers and loads skills from a sequence of directory roots.

    Roots are normalized to absolute paths on construction. Directories without a
    ``SKILL.md`` are ignored; malformed skills fail loudly with :class:`SkillsError`
    unless ``strict=False`` (used at the runtime composition boundary, where a single
    non-conforming skill must not break every other root).
    """

    def __init__(self, roots: Sequence[Path], *, strict: bool = True) -> None:
        self._roots: tuple[Path, ...] = tuple(Path(root).expanduser().resolve() for root in roots)
        self._strict = strict

    def discover(self) -> list[SkillMetadata]:
        """Parse every ``SKILL.md`` under the roots into metadata (level 1).

        Duplicate skill names across roots raise :class:`SkillsError`. The result is
        sorted by name for determinism.

        With ``strict=False`` (the runtime composition root uses it so one non-conforming
        vendored skill cannot poison discovery) a malformed skill or a duplicate name is
        skipped instead of raising.
        """
        by_name: dict[str, SkillMetadata] = {}
        for root in self._roots:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir() or not (child / "SKILL.md").is_file():
                    continue
                try:
                    metadata = _parse_skill(child)
                except SkillsError as exc:
                    if self._strict:
                        raise
                    _log.warning("skipping skill %s: %s", child, exc)
                    continue
                if metadata.name in by_name:
                    if self._strict:
                        raise SkillsError(
                            f"Duplicate skill name {metadata.name!r}: {metadata.path} and "
                            f"{by_name[metadata.name].path}"
                        )
                    continue
                by_name[metadata.name] = metadata
        return [by_name[name] for name in sorted(by_name)]

    def load(self, name: str) -> Skill:
        """Return the full skill — metadata plus body — for ``name`` (level 2).

        Raises :class:`SkillsError` for an unknown skill name.
        """
        metadata = self._find(name)
        skill_file = metadata.path / "SKILL.md"
        try:
            text = skill_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise SkillsError(f"Could not read {skill_file}: {exc}") from exc
        _fields, body = _parse_skill_file(text)
        return Skill(metadata=metadata, body=body.rstrip("\n"))

    def read_resource(self, name: str, rel_path: str) -> SkillResource:
        """Resolve ``rel_path`` inside the skill directory (level 3).

        Escapes (``..``, absolute paths, symlink hops that land outside the directory),
        unknown skills, and missing files all raise :class:`SkillsError`.
        """
        metadata = self._find(name)
        if Path(rel_path).is_absolute():
            raise SkillsError(
                f"Resource path must be relative to the skill directory: {rel_path!r}"
            )
        resolved = (metadata.path / rel_path).resolve()
        if not resolved.is_relative_to(metadata.path):
            raise SkillsError(f"Resource {rel_path!r} escapes skill directory {metadata.path}")
        if not resolved.is_file():
            raise SkillsError(f"Skill {name!r} has no resource file: {rel_path}")
        return SkillResource(name=name, path=resolved)

    def _find(self, name: str) -> SkillMetadata:
        for metadata in self.discover():
            if metadata.name == name:
                return metadata
        raise SkillsError(f"Unknown skill: {name!r}")
