"""A cross-project ideas inbox: plain Markdown files in ``<home>/ideas/``.

Conversations, memory and ``agent.db`` belong to one workspace; ideas do not. Each idea is one
file, ``<project>--<title-slug>.md``, so any tool (this agent, Claude Code, an editor, ``ls``)
can read and add to them. Nothing here talks to a database.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from langchain_core.tools import BaseTool, tool

__all__ = ["IdeaInbox", "idea_tools"]

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.strip().lower()).strip("-")[:60]


class IdeaInbox:
    def __init__(self, home: Path) -> None:
        self.root = Path(home).expanduser() / "ideas"

    def add(self, project: str, title: str, body: str) -> Path:
        project_slug, title_slug = _slug(project) or "general", _slug(title)
        if not title_slug:
            raise ValueError("an idea needs a title")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{project_slug}--{title_slug}.md"
        stamp = datetime.now(UTC).strftime("%Y-%m-%d")
        path.write_text(
            f"---\nproject: {project_slug}\ntitle: {title.strip()}\nadded: {stamp}\n---\n\n"
            f"{body.strip()}\n",
            encoding="utf-8",
        )
        return path

    def list(self, project: str | None = None) -> list[str]:
        """One line per idea (``project: title``), oldest file first; optionally one project."""
        wanted = _slug(project) if project else None
        lines: list[str] = []
        for path in sorted(self.root.glob("*.md")) if self.root.is_dir() else []:
            owner, _, rest = path.stem.partition("--")
            if wanted is None or owner == wanted:
                lines.append(f"{owner}: {rest}")
        return lines

    def read(self, project: str, title: str) -> str:
        path = self.root / f"{_slug(project) or 'general'}--{_slug(title)}.md"
        if not path.is_file():
            raise FileNotFoundError(f"no idea {project!r}/{title!r}")
        return path.read_text(encoding="utf-8")


def idea_tools(inbox: IdeaInbox) -> tuple[BaseTool, ...]:
    @tool
    async def add_idea(project: str, title: str, body: str) -> str:
        """Save an idea to the cross-project inbox, shared by every project and conversation.

        ``project`` is the project it belongs to (or "general"); ``body`` is markdown. Saving
        under an existing title replaces that idea.
        """
        try:
            inbox.add(project, title, body)
        except ValueError as exc:
            return f"Error: {exc}"
        return f"Saved idea {title!r} under {project!r}."

    @tool
    async def list_ideas(project: str = "") -> str:
        """List saved ideas as ``project: title`` lines, for one project or all of them."""
        return "\n".join(inbox.list(project or None)) or "No ideas saved yet."

    @tool
    async def read_idea(project: str, title: str) -> str:
        """Read one saved idea in full; ``title`` is the slug shown by list_ideas."""
        try:
            return inbox.read(project, title)
        except FileNotFoundError as exc:
            return f"Error: {exc}"

    return (add_idea, list_ideas, read_idea)
