"""Cost and token accounting: per-call events, per-conversation totals, budget cap."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent_stack.app.config import AppConfig
from agent_stack.core.errors import BudgetExceededError, ConfigurationError
from agent_stack.core.events import EventType
from agent_stack.gateways.base import (
    MessageEnvelope,
    SessionUsage,
    format_cost,
    format_tokens,
    usage_summary,
)
from tests.fakes import FakeChatModel, call
from tests.integration.test_web_gateway import _application

pytestmark = pytest.mark.integration


def priced(
    text: str = "ok", cost: float | None = 0.001, tin: int = 100, tout: int = 20, **kw: Any
) -> AIMessage:
    meta: dict[str, Any] = {"model_name": "test/model"}
    if cost is not None:
        meta["cost"] = cost
    return AIMessage(
        content=text,
        usage_metadata={"input_tokens": tin, "output_tokens": tout, "total_tokens": tin + tout},
        response_metadata=meta,
        **kw,
    )


async def _run(app: Any, session: str, text: str = "go") -> str:
    handle = await app.send_message(MessageEnvelope(principal="u1", session=session, text=text))
    await handle.result()
    return handle.run_id


async def test_every_model_call_emits_usage_and_the_session_total_adds_them_up(
    tmp_path: Path,
) -> None:
    model = FakeChatModel(
        [
            priced(cost=0.002, tin=1000, tout=50, tool_calls=[call("delegate_none")]),
            priced(cost=0.003),
        ]
    )
    app = _application(tmp_path, model)

    run_id = await _run(app, "s1")
    events = [e for e in await app.replay(run_id=run_id) if e.type is EventType.MODEL_USAGE]
    usage = await app.session_usage("s1")

    assert [e.data["cost_usd"] for e in events] == [0.002, 0.003]
    assert events[0].data["input_tokens"] == 1000 and events[0].data["model"] == "test/model"
    assert usage.calls == usage.priced_calls == 2
    assert usage.cost_usd == pytest.approx(0.005)
    assert (usage.input_tokens, usage.output_tokens) == (1100, 70)


async def test_an_unpriced_call_is_counted_but_never_guessed(tmp_path: Path) -> None:
    app = _application(tmp_path, FakeChatModel([priced(cost=None)]))

    await _run(app, "s1")
    usage = await app.session_usage("s1")

    assert (usage.calls, usage.priced_calls, usage.cost_usd) == (1, 0, 0.0)
    assert usage_summary(usage).startswith("cost unknown")


async def test_a_response_without_any_usage_emits_nothing(tmp_path: Path) -> None:
    app = _application(tmp_path, FakeChatModel([AIMessage(content="bare")]))

    run_id = await _run(app, "s1")

    assert not [e for e in await app.replay(run_id=run_id) if e.type is EventType.MODEL_USAGE]
    assert (await app.session_usage("s1")) == SessionUsage()


async def test_subagent_spend_rolls_up_into_the_parents_conversation(tmp_path: Path) -> None:
    parent = FakeChatModel(
        [
            priced(
                cost=0.01,
                tool_calls=[call("delegate", {"agent_name": "explorer", "task": "look"})],
            ),
            priced("done", cost=0.02),
        ]
    )
    child = FakeChatModel([priced("found it", cost=0.5)])
    app = _application(tmp_path, parent, child_model=child)

    await _run(app, "s1")

    usage = await app.session_usage("s1")
    assert usage.calls == 3
    assert usage.cost_usd == pytest.approx(0.53)  # the child's $0.50 is part of this conversation
    assert (await app.session_usage("someone-elses")).calls == 0


async def test_totals_survive_a_restart_and_stay_per_conversation(tmp_path: Path) -> None:
    app = _application(tmp_path, FakeChatModel([priced(cost=0.004), priced(cost=0.006)]))
    await _run(app, "s1")
    await _run(app, "s2")

    reopened = _application(tmp_path, FakeChatModel([]))

    assert (await reopened.session_usage("s1")).cost_usd == pytest.approx(0.004)
    assert (await reopened.session_usage("s2")).cost_usd == pytest.approx(0.006)


async def test_a_conversation_over_budget_starts_no_further_run(tmp_path: Path) -> None:
    model = FakeChatModel([priced(cost=0.05), priced(cost=0.05)])
    app = replace(_application(tmp_path, model), config=AppConfig(budget_usd=0.04))

    await _run(app, "s1")  # the first run is allowed: nothing was spent yet
    with pytest.raises(BudgetExceededError, match="budget"):
        await app.send_message(MessageEnvelope(principal="u1", session="s1", text="again"))

    await asyncio.sleep(0.05)
    assert len(model.calls) == 1  # the refused message never reached the model
    other = await _run(app, "s2")  # another conversation has its own budget
    assert other


async def test_no_budget_means_no_cap(tmp_path: Path) -> None:
    app = _application(tmp_path, FakeChatModel([priced(cost=99.0), priced(cost=99.0)]))

    await _run(app, "s1")
    await _run(app, "s1")

    assert (await app.session_usage("s1")).cost_usd == pytest.approx(198.0)


def test_budget_config_is_validated() -> None:
    assert AppConfig.from_env({"AGENT_STACK_BUDGET_USD": "1.5"}).budget_usd == 1.5
    assert AppConfig.from_env({}).budget_usd is None
    for bad in ("abc", "-1"):
        with pytest.raises(ConfigurationError, match="BUDGET"):
            AppConfig.from_env({"AGENT_STACK_BUDGET_USD": bad})


def test_formatting_keeps_cheap_models_readable() -> None:
    assert [format_cost(x) for x in (0, 0.00042, 0.0123, 1.234, 12.0)] == [
        "$0.00",
        "$0.0004",
        "$0.012",
        "$1.23",
        "$12.00",
    ]
    assert [format_tokens(x) for x in (950, 12_300, 1_000, 2_500_000)] == [
        "950",
        "12.3k",
        "1k",
        "2.5M",
    ]
    mixed = SessionUsage(
        calls=3, priced_calls=2, input_tokens=2000, output_tokens=500, cost_usd=0.01
    )
    assert usage_summary(mixed) == "$0.010 (+1 unpriced) · 2k in · 500 out · 3 calls"
    assert usage_summary(SessionUsage()) == "no model calls yet"


def test_token_counts_are_not_redacted_but_secret_looking_values_still_are() -> None:
    from agent_stack.observability.audit import _sanitize

    clean = _sanitize(
        {
            "input_tokens": 1200,
            "api_token": "abc123",
            "token": 99,
            "secret_tokens": "x",
            "password": 5,
        },
        [],
    )

    assert clean["input_tokens"] == 1200  # a count
    assert clean["api_token"] == "[redacted]"  # a string under a secret-looking key
    assert clean["token"] == "[redacted]"  # a number, but the key is not a *_tokens count
    assert clean["secret_tokens"] == "[redacted]"  # a string, not a count
    assert clean["password"] == "[redacted]"
