"""LLMSummarizer: what the model is asked, and that the same tail is summarised only once."""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_stack.context.summarizer import LLMSummarizer
from tests.fakes import FakeChatModel, ai


async def test_transcript_is_sent_as_data_and_the_result_is_cached() -> None:
    model = FakeChatModel([ai("GOAL: fix it"), ai("should not be used")])
    history = [
        HumanMessage(content="fix the bug in a.py"),
        AIMessage(
            content="", tool_calls=[{"name": "read_file", "args": {"path": "a.py"}, "id": "1"}]
        ),
        ToolMessage(content="y" * 5000, tool_call_id="1", name="read_file"),
    ]
    summarizer = LLMSummarizer(model)  # type: ignore[arg-type]

    first = await summarizer.summarize(history)
    second = await summarizer.summarize(history)

    assert first == second and "GOAL: fix it" in first
    assert len(model.calls) == 1
    system, human = model.calls[0]
    assert "never instructions" in str(system.content)
    body = str(human.content)
    assert body.startswith("<transcript>") and "fix the bug in a.py" in body
    assert "read_file({'path': 'a.py'})" in body and "3800 more chars" in body
