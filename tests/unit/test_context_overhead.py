from langchain_core.messages import HumanMessage
from langchain_core.tools import tool

from agent_stack.core.context import ContextBuilder, tool_schema_tokens
from agent_stack.gateways.base import SessionUsage, usage_summary


@tool
def lookup(query: str) -> str:
    """Look something up in a very large catalogue of things."""
    return query


def test_tool_schemas_are_counted() -> None:
    assert tool_schema_tokens([lookup]) > 10


def test_overhead_is_added_to_the_estimate() -> None:
    builder = ContextBuilder()
    msg = [HumanMessage(content="x" * 400)]
    base = builder.build(system_parts=["s"], history=msg).raw_estimate
    assert builder.build(system_parts=["s"], history=msg, overhead=500).raw_estimate == base + 500


def test_overhead_forces_earlier_dropping() -> None:
    builder = ContextBuilder(max_tokens=300, recent_window=50)
    history = [HumanMessage(content="y" * 200) for _ in range(10)]
    without = builder.build(system_parts=["s"], history=history)
    with_tools = builder.build(system_parts=["s"], history=history, overhead=200)
    assert len(with_tools.messages) < len(without.messages)


def test_calibration_ignores_the_fixed_overhead() -> None:
    builder = ContextBuilder()
    builder.calibrate(
        1500, 3500, overhead=1000
    )  # messages: 500 estimated, 2500 real -> 5x, clamped 3
    assert builder.scale == 2.0  # 0.5*1 + 0.5*3


def test_summary_shows_context_next_to_total() -> None:
    usage = SessionUsage().plus({"input_tokens": 1000}).plus({"input_tokens": 1200})
    assert usage.input_tokens == 2200 and usage.last_input_tokens == 1200
    assert "context 1.2k" in usage_summary(usage)
