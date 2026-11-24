"""bench/spend_guard.py: stops Harbor at the ceiling and survives a failed poll."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_PATH = Path(__file__).resolve().parents[2] / "bench" / "spend_guard.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("spend_guard", _PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stops_harbor_once_the_ceiling_is_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _load()
    readings = iter([0.5, 1.2, 2.01])

    def fake_usage(_key: str) -> float:
        value = next(readings)
        if value == 1.2:
            raise OSError("network down")  # a failed poll must not end the guard
        return value

    stopped: list[bool] = []
    monkeypatch.setattr(guard, "usage", fake_usage)
    monkeypatch.setattr(guard, "stop_harbor", lambda: stopped.append(True) or [1])
    monkeypatch.setattr(guard.time, "sleep", lambda _s: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(sys, "argv", ["spend_guard.py", "2.00", "1"])

    assert guard.main() == 3
    assert stopped == [True]


def test_does_not_stop_below_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    guard = _load()
    calls = {"n": 0}

    def fake_usage(_key: str) -> float:
        calls["n"] += 1
        if calls["n"] > 3:
            raise KeyboardInterrupt  # end the loop from the test
        return 0.5

    monkeypatch.setattr(guard, "usage", fake_usage)
    monkeypatch.setattr(guard, "stop_harbor", lambda: pytest.fail("must not stop"))
    monkeypatch.setattr(guard.time, "sleep", lambda _s: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(sys, "argv", ["spend_guard.py", "2.00"])

    with pytest.raises(KeyboardInterrupt):
        guard.main()
