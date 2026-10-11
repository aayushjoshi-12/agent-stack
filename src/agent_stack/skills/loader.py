"""Discover and parse ``SKILL.md`` files (P16b, level 1-3).

A skill is a directory containing a ``SKILL.md`` with YAML frontmatter followed by a
Markdown body:

* **Discovery** reads only the frontmatter into :class:`SkillMetadata` (level 1).
* **``load``** returns the full body (level 2).
* **``read_resource``** serves files from inside the skill directory (level 3).

The frontmatter subset is deliberately tiny. Only single-line ``key: value`` scalars
over a fixed key set are supported; block scalars (``|`` / ``>``), unknown keys, and
duplicate keys are rejected so malformed skills fail loudly at discovery instead of
being silently mis-parsed.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from agent_stack.core.errors import SkillsError
from agent_stack.skills.models import Skill, SkillMetadata, SkillResource

__all__ = ["MAX_DESCRIPTION_CHARS", "SkillLoader"]

#: Frontmatter keys this parser understands, all single-line scalar values. ``metadata``
#: (a nested map) is intentionally *not* part of the subset and is rejected as unsupported.
SUPPORTED_KEYS = frozenset({"name", "description", "license", "compatibility", "allowed-tools"})

#: Upper bound for a skill description per the ``SKILL.md`` spec.
MAX_DESCRIPTION_CHARS = 1024

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _parse_skill_file(text: str) -> tuple[dict[str, str], str]:
    """Parse ``SKILL.md`` frontmatter into ``(fields, body)``.

    The file must open with a ``---`` line and close with another; everything after the
    closing delimiter is the body, returned verbatim. Only single-line scalars over
    :data:`SUPPORTED_KEYS` are accepted: block scalars (``|``/``>``), unknown keys, and
    duplicate keys all raise :class:`SkillsError`.
    """
    if not text.startswith("---"):
        raise SkillsError("SKILL.md must start with a '---' frontmatter delimiter")

    fields: dict[str, str] = {}
    cursor = 3  # just past the opening "---"
    while True:
        newline = text.find("\n", cursor)
        if newline == -1:
            raise SkillsError("Unterminated frontmatter: missing closing '---'")
        line = text[cursor:newline].strip()
        cursor = newline + 1
        if line == "---":
            return fields, text[cursor:]
        if not line:
            continue
        if ":" not in line:
            raise SkillsError(f"Malformed frontmatter line (expected 'key: value'): {line!r}")
        key, _, raw_value = line.partition(":")
        field, value = key.strip(), raw_value.strip()
        if field not in SUPPORTED_KEYS:
            raise SkillsError(
                f"Unsupported frontmatter key {field!r} "
                f"(supported: {', '.join(sorted(SUPPORTED_KEYS))})"
            )
        if field in fields:
            raise SkillsError(f"Duplicate frontmatter key {field!r}")
        if value.startswith(("|", ">")):
            raise SkillsError(
                f"Unsupported multiline value for {field!r}: only single-line scalars are supported"
            )
        fields[field] = value


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
                except SkillsError:
                    if self._strict:
                        raise
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
