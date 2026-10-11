"""Unit tests for the cooperative cancellation token."""

from __future__ import annotations

import pytest

from agent_stack.core.cancellation import CancellationToken
from agent_stack.core.errors import RunCancelled


async def test_starts_not_cancelled() -> None:
    token = CancellationToken()

    assert not token.cancelled
    token.raise_if_cancelled()  # must be a no-op


async def test_cancel_sets_flag() -> None:
    token = CancellationToken()
    token.cancel()

    assert token.cancelled


async def test_cancel_is_idempotent() -> None:
    token = CancellationToken()
    token.cancel()
    token.cancel()

    assert token.cancelled


async def test_raise_if_cancelled_raises_run_cancelled() -> None:
    token = CancellationToken()
    token.cancel()

    with pytest.raises(RunCancelled):
        token.raise_if_cancelled()


async def test_distinct_tokens_are_independent() -> None:
    first = CancellationToken()
    second = CancellationToken()
    first.cancel()

    assert first.cancelled
    assert not second.cancelled
    second.raise_if_cancelled()  # unaffected by the other token