"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import dotenv
import pytest

from tests.fakes import FailingTool, FakeChatModel, SleepTool, ai

# `agent_stack.app.bootstrap` loads the developer's real `.env` when imported. Tests must never
# see real keys or tokens (a test could reach a live API), so loading is switched off here,
# before any test imports it.
dotenv.load_dotenv = lambda *args, **kwargs: False  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read or write the developer's real ``~/.agent-stack``."""
    monkeypatch.setenv("AGENT_STACK_HOME", str(tmp_path_factory.mktemp("agent-stack-home")))


@pytest.fixture
def tmp_workspace(tmp_path: Path) -> Path:
    """An isolated workspace; never the real home/working directory."""
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def final_model() -> FakeChatModel:
    """A model that immediately returns a final answer (no tools)."""
    return FakeChatModel([ai("done")])


@pytest.fixture
def sleep_tool() -> SleepTool:
    return SleepTool("sleepy", 0.25)


@pytest.fixture
def failing_tool() -> FailingTool:
    return FailingTool()
