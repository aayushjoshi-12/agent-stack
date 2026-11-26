"""Unit tests for :mod:`agent_stack.core.context`."""

from __future__ import annotations

from pathlib import Path

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent_stack.context import InstructionsLoader
from agent_stack.core.context import (
    COMPACTION_HEADER,
    ContextBuilder,
    after_compaction,
    compaction_message,
    estimate_tokens,
)


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


def _tool_turn(i: int, size: int = 10) -> list[BaseMessage]:
    call = {"name": "read_file", "args": {"path": f"f{i}"}, "id": f"c{i}", "type": "tool_call"}
    return [
        AIMessage(content="", tool_calls=[call]),
        ToolMessage(content="x" * size, tool_call_id=f"c{i}", name="read_file"),
    ]


def test_the_runs_request_survives_a_long_run() -> None:
    request = HumanMessage(content="THE GOAL")
    messages = [request] + [m for i in range(30) for m in _tool_turn(i)]  # 61 messages > window 40

    ctx = ContextBuilder(recent_window=40).build(
        system_parts=["base"], history=messages, pinned=request
    )

    assert ctx.messages[0] is request
    assert isinstance(ctx.messages[1], AIMessage)  # no orphaned tool result after the pin
    assert ctx.messages[-1] is messages[-1]


def test_old_tool_results_are_cleared_before_anything_is_dropped() -> None:
    request = HumanMessage(content="goal")
    messages = [request] + [m for i in range(12) for m in _tool_turn(i, size=4000)]
    builder = ContextBuilder(recent_window=100, max_tokens=6000, keep_tool_results=3)

    ctx = builder.build(system_parts=["base"], history=messages, pinned=request)

    tools = [m for m in ctx.messages if isinstance(m, ToolMessage)]
    assert len(tools) == 12  # every call still has its result: the pairs stay valid
    assert all("cleared" in str(m.content) for m in tools[:-3])
    assert all(str(m.content).startswith("x") for m in tools[-3:])
    assert all(m.tool_call_id == f"c{i}" for i, m in enumerate(tools))
    assert str(messages[2].content).startswith("x")  # the stored original is untouched


def test_still_over_budget_drops_oldest_but_keeps_the_pin() -> None:
    request = HumanMessage(content="goal")
    messages = [request] + [m for i in range(40) for m in _tool_turn(i, size=200)]
    builder = ContextBuilder(recent_window=1000, max_tokens=200, keep_tool_results=2)

    ctx = builder.build(system_parts=["base"], history=messages, pinned=request)

    assert ctx.messages[0] is request
    assert 3 <= len(ctx.messages) < 12
    assert "earlier messages were dropped" in str(ctx.messages[1].content)  # the model is told
    assert estimate_tokens(ctx.messages, len("base")) <= 200
    assert isinstance(ctx.messages[2], AIMessage)  # no orphaned tool result after the note
    assert ctx.messages[-1] is messages[-1]


def test_calibrate_makes_the_budget_stricter_when_the_estimate_undercounts() -> None:
    from langchain_core.messages import AIMessage

    builder = ContextBuilder(max_tokens=1000, recent_window=100, keep_tool_results=0)
    history: list = [HumanMessage(content="go")]
    for i in range(30):
        history += [AIMessage(content="x" * 100), HumanMessage(content=f"{i}" * 100)]
    before = len(builder.build(system_parts=["s"], history=history).messages)
    ctx = builder.build(system_parts=["s"], history=history)
    builder.calibrate(ctx.raw_estimate, ctx.raw_estimate * 2)  # the provider saw twice as many
    assert builder.scale > 1.2
    after = len(builder.build(system_parts=["s"], history=history).messages)
    assert after < before


def test_calibrate_ignores_nonsense_readings() -> None:
    builder = ContextBuilder(max_tokens=1000)
    builder.calibrate(0, 500)
    builder.calibrate(500, 0)
    builder.calibrate(1000, 10)  # real lower than estimate: never loosens past 1.0
    assert builder.scale == 1.0


def test_compaction_marker_hides_everything_before_it() -> None:
    old, newer = HumanMessage(content="old"), HumanMessage(content="newer")
    first = compaction_message("GOAL: a")
    second = compaction_message("GOAL: b")

    summary, rest = after_compaction([old, first, HumanMessage(content="mid"), second, newer])

    assert summary is not None and summary.startswith(COMPACTION_HEADER) and "GOAL: b" in summary
    assert rest == [newer]
    assert after_compaction([old, newer]) == (None, [old, newer])
    # an ordinary system message is not a marker
    assert after_compaction([SystemMessage(content="x"), old])[0] is None


def test_over_threshold_compares_against_the_share_of_the_budget() -> None:
    builder = ContextBuilder(max_tokens=1000, compact_at=0.5)
    assert not builder.over_threshold(["s"], [HumanMessage(content="x" * 1900)])  # ~475 tokens
    assert builder.over_threshold(["s"], [HumanMessage(content="x" * 2100)])  # ~525 tokens
    assert not ContextBuilder().over_threshold(["s"], [HumanMessage(content="x" * 10**6)])
    off = ContextBuilder(max_tokens=1000, compact_at=None)  # AGENT_STACK_COMPACT_AT=off
    assert not off.over_threshold(["s"], [HumanMessage(content="x" * 10**6)])
