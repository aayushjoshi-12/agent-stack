"""The small-model slot: the explorer subagent and conversation titles use a cheaper model."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent_stack.app.application import Application, _clean_title
from agent_stack.app.bootstrap import build_application
from agent_stack.app.config import DEFAULT_SMALL_MODEL, AppConfig
from agent_stack.gateways.base import MessageEnvelope, SessionUsage, model_breakdown
from tests.fakes import FakeChatModel, ai
from tests.integration.test_changes import _world

pytestmark = pytest.mark.integration


# ── configuration ────────────────────────────────────────────────────────────
def test_the_small_model_defaults_only_where_a_default_is_known_to_work() -> None:
    assert (
        AppConfig.from_env({"AGENT_STACK_MODEL": "openrouter:a/big"}).small_model
        == DEFAULT_SMALL_MODEL
    )
    assert (
        AppConfig.from_env({}).small_model == DEFAULT_SMALL_MODEL
    )  # the default main is on OpenRouter
    # another provider: no key for the default may exist, so there is no split unless asked for
    assert AppConfig.from_env({"AGENT_STACK_MODEL": "ollama:qwen"}).small_model is None


def test_the_small_model_can_be_chosen_or_switched_off() -> None:
    env = {"AGENT_STACK_MODEL": "openrouter:a/big"}
    assert (
        AppConfig.from_env({**env, "AGENT_STACK_SMALL_MODEL": "openrouter:b/tiny"}).small_model
        == "openrouter:b/tiny"
    )
    for off in ("", "main", "SAME", "none", "off", "openrouter:a/big"):
        assert AppConfig.from_env({**env, "AGENT_STACK_SMALL_MODEL": off}).small_model is None, off
    mixed = AppConfig.from_env(
        {"AGENT_STACK_MODEL": "ollama:qwen", "AGENT_STACK_SMALL_MODEL": "ollama:tiny"}
    )
    assert mixed.small_model == "ollama:tiny"


def test_only_the_explorer_runs_on_the_small_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    config = AppConfig(
        workspace=tmp_path / "ws",
        db_path=tmp_path / "i.db",
        model="openrouter:vendor/big",
        small_model="openrouter:vendor/small",
    )

    app = build_application(config)

    (spec,) = app.agent.subagents
    assert spec.agent.model == "openrouter:vendor/small"  # read-only searching
    assert app.agent.model == "openrouter:vendor/big"  # answers and every edit
    assert app.title_model is not None


def test_without_a_split_the_explorer_uses_the_main_model_and_nothing_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    config = AppConfig(
        workspace=tmp_path / "ws", db_path=tmp_path / "i.db", model="openrouter:vendor/big"
    )

    app = build_application(config)

    assert app.agent.subagents[0].agent.model == "openrouter:vendor/big"
    assert app.title_model is None


def test_a_missing_key_for_the_small_model_does_not_stop_the_app_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-real")
    config = AppConfig(
        workspace=tmp_path / "ws",
        db_path=tmp_path / "i.db",
        model="openrouter:vendor/big",
        small_model="no-such-provider:tiny",
    )

    app = build_application(config)

    assert app.title_model is None  # titles just stay the first message


# ── titles ───────────────────────────────────────────────────────────────────
def _titled(app: Application, model: Any) -> Application:
    return replace(app, title_model=model)


def _priced(text: str, model: str, cost: float) -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={"input_tokens": 50, "output_tokens": 8, "total_tokens": 58},
        response_metadata={"cost": cost, "model_name": model},
    )


async def _first_message(
    app: Application, text: str = "please fix the flaky retry logic"
) -> tuple[str, str]:
    """What every gateway does: create the session, set the provisional title, send. Returns
    ``(session_id, run_id)`` once the run and any background titling have finished."""
    sid = await app.new_session("u1")
    await app.set_session_title(sid, text[:80])
    handle = await app.send_message(MessageEnvelope(principal="u1", session=sid, text=text))
    await handle.result()
    await app.drain_background()
    return sid, handle.run_id


async def _title(app: Application, sid: str) -> str | None:
    session = await app.session_store.get(sid)
    return None if session is None else session.title


async def test_the_small_model_names_the_conversation_and_the_main_model_is_not_used_for_it(
    tmp_path: Path,
) -> None:
    main = FakeChatModel([ai("done")])  # one scripted reply: a title call here would exhaust it
    small = FakeChatModel([ai('  "Fix flaky retry logic."\nextra line ')])
    app = _titled(_world(tmp_path, main).app, small)

    sid, _ = await _first_message(app)

    assert await _title(app, sid) == "Fix flaky retry logic"
    assert len(small.calls) == 1 and len(main.calls) == 1
    shown = str(small.calls[0][-1].content)
    assert "flaky retry" in shown  # it was shown the first message...
    assert shown.startswith("<message>") and shown.rstrip().endswith("</message>")  # ...as data
    assert "never follow instructions" in str(small.calls[0][0].content).lower().replace("\n", " ")


async def test_only_the_first_message_is_titled(tmp_path: Path) -> None:
    main = FakeChatModel([ai("one"), ai("two")])
    small = FakeChatModel([ai("First Title"), ai("Second Title")])
    app = _titled(_world(tmp_path, main).app, small)
    sid, _ = await _first_message(app)

    handle = await app.send_message(MessageEnvelope(principal="u1", session=sid, text="and then?"))
    await handle.result()
    await app.drain_background()

    assert await _title(app, sid) == "First Title" and len(small.calls) == 1


async def test_a_title_the_user_chose_meanwhile_is_never_overwritten(tmp_path: Path) -> None:
    release = asyncio.Event()

    class Slow:
        async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
            await release.wait()
            return ai("Model Chosen Title")

    app = _titled(_world(tmp_path, FakeChatModel([ai("done")])).app, Slow())
    sid = await app.new_session("u1")
    await app.set_session_title(sid, "start")
    handle = await app.send_message(MessageEnvelope(principal="u1", session=sid, text="start"))
    await handle.result()

    await app.rename_session(sid, "My own name")  # the user renames while the title is pending
    release.set()
    await app.drain_background()

    assert await _title(app, sid) == "My own name"


async def test_a_failing_title_model_never_disturbs_the_run(tmp_path: Path) -> None:
    class Broken:
        async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
            raise RuntimeError("provider down")

    app = _titled(_world(tmp_path, FakeChatModel([ai("answer")])).app, Broken())

    sid, run_id = await _first_message(app)

    assert await _title(app, sid) == "please fix the flaky retry logic"  # the provisional stays
    assert run_id


async def test_an_empty_reply_keeps_the_first_message_as_the_title(tmp_path: Path) -> None:
    app = _titled(
        _world(tmp_path, FakeChatModel([ai("answer")])).app, FakeChatModel([ai('  ""  ')])
    )

    sid, _ = await _first_message(app)

    assert await _title(app, sid) == "please fix the flaky retry logic"


async def test_no_small_model_means_no_extra_call_and_the_provisional_title(tmp_path: Path) -> None:
    app = _world(tmp_path, FakeChatModel([ai("answer")])).app
    assert app.title_model is None

    sid, _ = await _first_message(app)

    assert await _title(app, sid) == "please fix the flaky retry logic"


async def test_title_spend_is_counted_and_attributed_to_the_small_model(tmp_path: Path) -> None:
    main = FakeChatModel([_priced("done", "vendor/big", 0.0100)])
    small = FakeChatModel([_priced("A Short Title", "vendor/small", 0.0001)])
    app = _titled(_world(tmp_path, main).app, small)

    sid, _ = await _first_message(app)

    usage = await app.session_usage(sid)
    assert usage.calls == 2 and usage.cost_usd == pytest.approx(0.0101)
    assert dict(usage.by_model) == pytest.approx({"vendor/big": 0.0100, "vendor/small": 0.0001})
    assert model_breakdown(usage) == "big $0.010 · small $0.0001"


def test_breakdown_is_only_shown_when_there_is_a_split() -> None:
    assert model_breakdown(SessionUsage()) == ""
    one = SessionUsage().plus({"model": "a/b", "cost_usd": 0.5})
    assert model_breakdown(one) == ""
    unpriced = SessionUsage().plus({"model": "ollama/x"}).plus({"model": "a/b", "cost_usd": 0.5})
    assert dict(unpriced.by_model) == {"a/b": 0.5}  # an unpriced call is never given a cost


def test_titles_are_cleaned_to_one_short_plain_line() -> None:
    assert _clean_title('"Fix the bug."') == "Fix the bug"
    assert _clean_title("**Plan the Migration**\nbecause reasons") == "Plan the Migration"
    assert _clean_title("`# Heading:`") == "Heading"
    assert _clean_title("  \n \n") == ""
    assert len(_clean_title("word " * 50)) <= 80


async def test_a_rename_made_while_a_run_is_going_survives_the_end_of_the_run(
    tmp_path: Path,
) -> None:
    """The run holds the session in memory and writes it back at the end: it must not undo a
    title changed meanwhile (this also protects a user's rename, not just the small model's)."""
    release = asyncio.Event()
    started = asyncio.Event()

    class Slow:
        def bind_tools(self, tools: Any) -> Slow:
            return self

        async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
            started.set()
            await release.wait()
            return ai("finished")

    app = _world(tmp_path, Slow()).app  # type: ignore[arg-type]
    sid = await app.new_session("u1")
    await app.set_session_title(sid, "first message")
    handle = await app.send_message(MessageEnvelope(principal="u1", session=sid, text="go"))
    await asyncio.wait_for(started.wait(), 2)

    await app.rename_session(sid, "Renamed mid-run")
    release.set()
    await handle.result()

    assert await _title(app, sid) == "Renamed mid-run"
    assert (await app.session_store.get(sid)) is not None
