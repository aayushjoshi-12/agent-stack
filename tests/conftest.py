"""Shared pytest fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.fakes import FailingTool, FakeChatModel, SleepTool, ai


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
