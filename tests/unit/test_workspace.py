"""Unit tests for :class:`agent_stack.core.workspace.WorkspacePolicy`."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_stack.core.errors import ConfigurationError, WorkspaceEscapeError
from agent_stack.core.workspace import WorkspacePolicy


def test_resolve_inside_root(tmp_workspace: Path) -> None:
    policy = WorkspacePolicy(tmp_workspace)

    assert policy.resolve("a/b.txt") == policy.root / "a" / "b.txt"
    assert policy.resolve(Path("notes.md")) == policy.root / "notes.md"


def test_resolve_accepts_absolute_path_inside_root(tmp_workspace: Path) -> None:
    policy = WorkspacePolicy(tmp_workspace)

    assert policy.resolve(policy.root / "inside.txt") == policy.root / "inside.txt"


def test_rejects_parent_escape(tmp_workspace: Path) -> None:
    policy = WorkspacePolicy(tmp_workspace)

    with pytest.raises(WorkspaceEscapeError):
        policy.resolve("../secret.txt")


def test_rejects_absolute_path_outside_root(tmp_path: Path, tmp_workspace: Path) -> None:
    policy = WorkspacePolicy(tmp_workspace)
    outside = tmp_path / "outside.txt"

    with pytest.raises(WorkspaceEscapeError):
        policy.resolve(outside)


def test_rejects_nested_escape(tmp_workspace: Path) -> None:
    policy = WorkspacePolicy(tmp_workspace)

    with pytest.raises(WorkspaceEscapeError):
        policy.resolve("a/../../secret.txt")


def test_allow_external_permits_outside(tmp_path: Path, tmp_workspace: Path) -> None:
    policy = WorkspacePolicy(tmp_workspace, allow_external=True)
    outside = tmp_path / "outside.txt"

    assert policy.resolve(outside) == outside.resolve()


def test_relative_round_trips(tmp_workspace: Path) -> None:
    policy = WorkspacePolicy(tmp_workspace)
    resolved = policy.resolve("a/b.txt")

    assert policy.relative(resolved) == "a/b.txt"


def test_root_must_exist_and_be_directory(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        WorkspacePolicy(tmp_path / "missing")

    file_path = tmp_path / "a-file"
    file_path.write_text("x")
    with pytest.raises(ConfigurationError):
        WorkspacePolicy(file_path)
