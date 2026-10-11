"""Unit tests for the frozen `Agent` definition."""

from __future__ import annotations

import dataclasses

import pytest

from agent_stack.core.agent import Agent, SubAgentSpec
from agent_stack.core.limits import AgentLimits


def test_agent_is_frozen() -> None:
    agent = Agent(name="a", model="m")
    with pytest.raises(dataclasses.FrozenInstanceError):
        agent.name = "b"


def test_agent_defaults() -> None:
    agent = Agent(name="a", model="m")
    assert agent.tools == ()
    assert agent.subagents == ()
    assert agent.system_prompt == ""
    assert agent.limits == AgentLimits()


def test_agent_limits_defaults() -> None:
    limits = AgentLimits()
    assert limits.max_steps == 50
    assert limits.max_tool_calls == 100
    assert limits.max_runtime_seconds == 1800.0
    assert limits.max_concurrent_tools == 8
    assert limits.max_subagent_depth == 2
    assert limits.max_subagents == 8


def test_subagent_spec_holds_nested_agent() -> None:
    child = Agent(name="child", model="m")
    spec = SubAgentSpec(name="child", description="does child things", agent=child)
    assert spec.name == "child"
    assert spec.description == "does child things"
    assert spec.agent is child
