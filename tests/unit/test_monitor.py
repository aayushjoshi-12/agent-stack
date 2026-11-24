from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent_stack.core.agent import Agent
from agent_stack.core.run_context import RunContext, run_context
from agent_stack.monitor import MonitorService, monitor_tools
from agent_stack.monitor.service import MonitorError
from agent_stack.tools.builtins.terminal import CommandResult


def _result(out: str = "", code: int = 0) -> CommandResult:
    return CommandResult(exit_code=code, stdout=out, stderr="", duration_ms=1.0, timed_out=False)


class Script:
    """A runner that returns scripted outputs, then repeats the last one."""

    def __init__(self, *results: CommandResult) -> None:
        self.results = list(results)
        self.calls = 0

    async def __call__(self, command: str, cwd: Path) -> CommandResult:
        self.calls += 1
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


async def _until(predicate, timeout: float = 2.0) -> None:  # type: ignore[no-untyped-def]
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def _service(runner: Script) -> MonitorService:
    return MonitorService(Path("."), runner=runner, min_interval=0.01)


async def test_change_mode_is_silent_until_the_output_changes() -> None:
    runner = Script(_result("a"), _result("a"), _result("a"), _result("b"))
    service = _service(runner)
    service.start("s1", "x", mode="change", interval=0.01)
    await _until(lambda: service.peek("s1") == 1)
    note = service.drain("s1")
    assert note and "before:\na" in note and "after:\nb" in note
    assert service.drain("s1") is None
    await service.stop_all()


async def test_match_mode_notes_each_distinct_match_once() -> None:
    runner = Script(_result("ok"), _result("ERROR 1"), _result("ERROR 1"), _result("ERROR 2"))
    service = _service(runner)
    service.start("s1", "x", mode="match", pattern="ERROR", interval=0.01)
    await _until(lambda: service.peek("s1") == 2)
    await asyncio.sleep(0.05)
    assert service.peek("s1") == 2
    await service.stop_all()


async def test_exit_mode_ends_the_watch_on_success() -> None:
    runner = Script(_result("", 1), _result("", 1), _result("done", 0))
    service = _service(runner)
    watch = service.start("s1", "test -f done", mode="exit", interval=0.01)
    await _until(lambda: watch.status == "done")
    assert "succeeded" in (service.drain("s1") or "")
    calls = runner.calls
    await asyncio.sleep(0.05)
    assert runner.calls == calls  # no further polls


async def test_a_watch_stops_after_its_note_limit_and_after_its_time() -> None:
    changing = Script(*[_result(str(i)) for i in range(50)])
    service = _service(changing)
    watch = service.start("s1", "x", interval=0.01, max_notes=2)
    await _until(lambda: watch.status == "done")
    assert watch.notes == 3  # two changes plus the "reached its limit" note
    quiet = _service(Script(_result("a")))
    expiring = quiet.start("s2", "x", interval=0.01, max_minutes=0.0001)
    await _until(lambda: expiring.status == "expired")


async def test_requests_are_validated_and_scoped_to_the_session() -> None:
    service = _service(Script(_result("a")))
    with pytest.raises(MonitorError):
        service.start("s1", "x", mode="bogus")
    with pytest.raises(MonitorError):
        service.start("s1", "x", mode="match")
    with pytest.raises(MonitorError):
        service.start("s1", "x", mode="match", pattern="(")
    with pytest.raises(MonitorError):
        service.start("s1", "x", interval=0.0001)
    watch = service.start("s1", "x", interval=0.01)
    assert not service.stop("other-session", watch.id)
    assert service.stop("s1", watch.id)
    await service.stop_all()


async def test_tools_work_inside_a_run_and_refuse_outside_paths(tmp_path: Path) -> None:
    service = MonitorService(tmp_path, runner=Script(_result("a")), min_interval=0.01)
    tools = {t.name: t for t in monitor_tools(service, tmp_path)}
    assert "only available inside a run" in await tools["monitor_list"].ainvoke({})
    context = RunContext(
        run_id="r", session_id="s1", user_id="u", depth=0, parent_run_id=None,
        agent=Agent(name="a", model="test:m"), services=None,  # type: ignore[arg-type]
    )  # fmt: skip
    with run_context(context):
        out = await tools["monitor_start"].ainvoke(
            {"command": "echo hi", "interval_seconds": 0.01}
        )
        assert out.startswith("Started mon-")
        assert "outside the workspace" in await tools["monitor_start"].ainvoke(
            {"command": "tail -f /Users/x/log"}
        )
        assert "mon-" in await tools["monitor_list"].ainvoke({})
        assert await tools["monitor_notes"].ainvoke({}) == "No new monitor notes."
    await service.stop_all()
