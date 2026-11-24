from __future__ import annotations

from pathlib import Path

from agent_stack.memory.ideas import IdeaInbox, idea_tools


def test_ideas_from_different_projects_share_one_inbox(tmp_path: Path) -> None:
    inbox = IdeaInbox(tmp_path)
    inbox.add("rag-pipeline", "Hybrid search", "BM25 + vectors")
    inbox.add("Agent Stack", "Local models!", "try ollama")

    assert inbox.list() == ["agent-stack: local-models", "rag-pipeline: hybrid-search"]
    assert inbox.list("rag-pipeline") == ["rag-pipeline: hybrid-search"]
    assert "BM25 + vectors" in inbox.read("rag-pipeline", "hybrid-search")
    assert (tmp_path / "ideas" / "rag-pipeline--hybrid-search.md").read_text().startswith("---\n")


async def test_tools_never_raise(tmp_path: Path) -> None:
    add, listing, read = idea_tools(IdeaInbox(tmp_path))

    assert "Error" in await add.ainvoke({"project": "p", "title": "!!", "body": "x"})
    assert await listing.ainvoke({}) == "No ideas saved yet."
    assert "Error" in await read.ainvoke({"project": "p", "title": "nope"})
    assert "Saved" in await add.ainvoke({"project": "p", "title": "Real one", "body": "x"})
    assert await listing.ainvoke({"project": "p"}) == "p: real-one"
