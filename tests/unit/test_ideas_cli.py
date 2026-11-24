from pathlib import Path

import pytest

from agent_stack.observability.cli import main


def test_ideas_add_list_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_STACK_HOME", str(tmp_path))
    assert main(["ideas", "list"]) == 0
    assert "No ideas yet." in capsys.readouterr().out
    assert main(["ideas", "add", "rag", "Cache warmup", "warm the cache on deploy"]) == 0
    assert main(["ideas", "add", "other", "Thing", "x"]) == 0
    capsys.readouterr()
    assert main(["ideas", "list", "--project", "rag"]) == 0
    assert capsys.readouterr().out.strip() == "rag: cache-warmup"
    assert main(["ideas", "show", "rag", "Cache warmup"]) == 0
    assert "warm the cache on deploy" in capsys.readouterr().out
    assert main(["ideas", "show", "rag", "missing"]) == 1
