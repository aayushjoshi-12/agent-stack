"""Round-trip tests for the LangChain message ⇄ JSONL record serializers.

Tool plumbing (``tool_calls`` / ``tool_call_id``) must survive the round trip exactly, or a
recovered conversation breaks the model's tool loop.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from agent_stack.persistence.serializers import (
    MessageRecord,
    message_to_record,
    record_from_json,
    record_to_json,
    record_to_message,
)

TS = datetime(2026, 10, 11, 12, 0, 0, tzinfo=UTC)


def _round_trip(msg: BaseMessage) -> BaseMessage:
    record = message_to_record(msg, seq=7, run_id="r1", ts=TS)
    restored = record_from_json(record_to_json(record))
    return record_to_message(restored)


def _tool_call(name: str, args: dict[str, Any], id: str) -> dict[str, Any]:
    return {"id": id, "name": name, "args": args, "type": "tool_call"}


def test_human_round_trip() -> None:
    restored = _round_trip(HumanMessage(content="run the tests"))
    assert isinstance(restored, HumanMessage)
    assert restored.content == "run the tests"


def test_ai_round_trip_preserves_tool_calls() -> None:
    calls: list[dict[str, Any]] = [
        _tool_call("terminal", {"command": "pytest"}, "call-1"),
        _tool_call("read_file", {"path": "a.py"}, "call-2"),
    ]
    original = AIMessage(content="", tool_calls=calls, id="ai-1")

    restored = _round_trip(original)

    assert isinstance(restored, AIMessage)
    assert restored.content == ""
    assert restored.tool_calls == calls


def test_tool_round_trip_preserves_tool_call_id() -> None:
    restored = _round_trip(ToolMessage(content="12 passed", tool_call_id="call-1"))
    assert isinstance(restored, ToolMessage)
    assert restored.content == "12 passed"
    assert restored.tool_call_id == "call-1"


def test_system_round_trip() -> None:
    restored = _round_trip(SystemMessage(content="be boring"))
    assert isinstance(restored, SystemMessage)
    assert restored.content == "be boring"


def test_record_fields_survive_json() -> None:
    record = MessageRecord(
        seq=3,
        ts=TS,
        run_id="r7",
        kind="ai",
        content="hi",
        tool_calls=(_tool_call("terminal", {"command": "pwd"}, "c1"),),
        tool_call_id=None,
        data={"channel": "cli"},
    )

    restored = record_from_json(record_to_json(record))

    assert restored == record
    assert restored.ts == TS
    assert restored.seq == 3
    assert restored.run_id == "r7"
    assert restored.kind == "ai"
    assert restored.tool_calls == record.tool_calls
    assert restored.data == {"channel": "cli"}


def test_multiline_content_is_single_json_line() -> None:
    record = message_to_record(
        HumanMessage(content="line one\nline two"),
        seq=1,
        run_id="r1",
        ts=TS,
    )
    line = record_to_json(record)
    assert "\n" not in line
    assert record_from_json(line) == record


def test_sequence_of_messages_keeps_order_and_seqs() -> None:
    messages: Sequence[BaseMessage] = [
        HumanMessage(content="first"),
        AIMessage(content="second", tool_calls=[_tool_call("ls", {"p": "."}, "c9")]),
        ToolMessage(content="ok", tool_call_id="c9"),
    ]
    records = [
        message_to_record(message, seq=offset + 1, run_id="r1", ts=TS)
        for offset, message in enumerate(messages)
    ]
    assert [record.seq for record in records] == [1, 2, 3]
    assert [record.kind for record in records] == ["human", "ai", "tool"]
    assert records[2].tool_call_id == "c9"
    assert records[2].run_id == "r1"
