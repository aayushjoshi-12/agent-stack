"""Unit tests for :mod:`agent_stack.core.context`."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from agent_stack.context import InstructionsLoader
from agent_stack.core.context import ContextBuilder


class FakeSummarizer:
    """Records the tail it summarized and returns a fixed summary."""

    def __init__(self) -> None:
        self.summarized: list[Sequence[BaseMessage]] = []

    async def summarize(self, messages: Sequence[BaseMessage]) -> str:
        self.summarized.append(messages)
        return "SUMMARY"


def _history(count: int) -> list[BaseMessage]:
    return [HumanMessage(content=f"message-{i}") for i in range(count)]


def test_context_builds_system_plus_recent_messages() -> None:
    builder = ContextBuilder(recent_window=10)
    history = _history(50)

    ctx = builder.build(system_parts=("base prompt", "instructions", "memory"), history=history)

    # System parts are ordered exactly as given; messages are the last window.
    assert ctx.system == ("base prompt", "instructions", "memory")
    assert len(ctx.messages) == 10
    assert ctx.messages == tuple(history[-10:])
    assert ctx.messages[0].content == "message-40"
    assert ctx.messages[-1].content == "message-49"


async def test_large_conversation_is_compacted() -> None:
    summarizer = FakeSummarizer()
    builder = ContextBuilder(recent_window=10, summarizer=summarizer)
    history = _history(50)

    compacted = await builder.compact(history)

    # One summary SystemMessage followed by the recent 10 messages.
    assert len(compacted) == 11
    assert isinstance(compacted[0], SystemMessage)
    assert compacted[0].content == "SUMMARY"
    assert compacted[1:] == history[-10:]
    assert summarizer.summarized == [history[:-10]]


async def test_compact_without_summarizer_drops_old_tail() -> None:
    builder = ContextBuilder(recent_window=10)
    history = _history(50)

    compacted = await builder.compact(history)

    assert compacted == history[-10:]
    assert len(compacted) == 10


async def test_compact_short_history_unchanged() -> None:
    builder = ContextBuilder(recent_window=10)
    history = _history(5)

    compacted = await builder.compact(history)

    assert compacted == history
    assert len(compacted) == 5


async def test_instructions_injected_into_system_prompt(tmp_path: Path) -> None:
    project = tmp_path / "project"
    nested = project / "src"
    nested.mkdir(parents=True)
    (project / "AGENTS.md").write_text("Use tabs, not spaces.", encoding="utf-8")
    (nested / "AGENTS.md").write_text("Write tests with pytest.", encoding="utf-8")

    loader = InstructionsLoader()
    files = await loader.load(nested, stop_at=project)
    parts = [f"# {file.path.name}\n\n{file.content}" for file in files]

    builder = ContextBuilder(recent_window=5)
    history = [HumanMessage(content="hi"), AIMessage(content="hello")]
    ctx = builder.build(system_parts=("base prompt", *parts), history=history)

    combined = "\n\n".join(ctx.system)
    assert combined.startswith("base prompt")
    assert "Write tests with pytest." in combined
    assert "Use tabs, not spaces." in combined
    # Nearest file is injected first.
    assert combined.index("Write tests with pytest.") < combined.index("Use tabs, not spaces.")
    assert ctx.messages == (
        HumanMessage(content="hi"),
        AIMessage(content="hello"),
    )