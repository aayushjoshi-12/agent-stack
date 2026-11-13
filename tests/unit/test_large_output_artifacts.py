"""P15: large tool output lives outside the model's context; the model sees a reference."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_stack.artifacts.base import ArtifactRef
from agent_stack.artifacts.local import LocalArtifactStore
from agent_stack.core.errors import ArtifactError
from agent_stack.tools.builtins.artifacts import artifact_tools
from agent_stack.tools.executor import ARTIFACT_THRESHOLD, ToolExecutor
from agent_stack.tools.registry import ToolRegistry


class Emit:
    name = "emit"
    description = "emit text"

    def __init__(self, text: str) -> None:
        self._text = text

    async def ainvoke(self, args: dict[str, Any]) -> str:
        return self._text


async def _run(text: str, store: Any, **kwargs: Any) -> str:
    executor = ToolExecutor(ToolRegistry([Emit(text)]), artifacts=store, **kwargs)  # type: ignore[list-item]
    result = await executor.execute_one({"name": "emit", "args": {}, "id": "c1"})
    return str(result.message.content)


def _artifact_id(message: str) -> str:
    return message.split("artifact://", 1)[1].split(";", 1)[0].strip()


async def test_large_output_becomes_artifact_and_short_message(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")
    big = "".join(f"line {i}\n" for i in range(40_000))  # ~0.4 MB

    message = await _run(big, store)

    assert len(message.encode()) < 6_000  # a short message, not the whole output
    assert message.startswith("line 0\nline 1\n")  # with a preview of the beginning
    assert "artifact://" in message and "read_artifact" in message
    ref = ArtifactRef(
        id=_artifact_id(message), name="x", mime_type="text/plain", size_bytes=0, uri=""
    )
    assert (await store.get(ref)).decode() == big  # and the full output is intact


async def test_small_output_is_untouched_and_nothing_is_stored(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path / "artifacts")

    assert await _run("x" * (ARTIFACT_THRESHOLD - 1), store) == "x" * (ARTIFACT_THRESHOLD - 1)
    assert not (tmp_path / "artifacts").exists()


async def test_the_stored_artifact_never_contains_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEMO_API_TOKEN", "zebra-quartz-9921!")
    store = LocalArtifactStore(tmp_path / "artifacts")

    message = await _run("pw zebra-quartz-9921! " + "filler " * 10_000, store)

    ref = ArtifactRef(
        id=_artifact_id(message), name="x", mime_type="text/plain", size_bytes=0, uri=""
    )
    assert b"zebra-quartz" not in await store.get(ref)
    assert "zebra-quartz" not in message


async def test_a_failing_store_falls_back_to_truncation() -> None:
    class Broken:
        async def put(self, **kwargs: Any) -> ArtifactRef:
            raise ArtifactError("disk full")

    message = await _run("y" * 300_000, Broken())

    assert len(message.encode()) <= 100_000 and "truncated" in message.lower()


async def test_without_a_store_large_output_is_still_capped() -> None:
    message = await _run("z" * 300_000, None)

    assert len(message.encode()) <= 100_000


# ── read_artifact ────────────────────────────────────────────────────────────
async def _stored(tmp_path: Path, text: str) -> tuple[Any, str]:
    store = LocalArtifactStore(tmp_path / "artifacts")
    message = await _run(text, store)
    (read_artifact,) = artifact_tools(store)
    return read_artifact, _artifact_id(message)


async def test_read_artifact_pages_through_the_stored_output(tmp_path: Path) -> None:
    text = "".join(f"{i:06d}\n" for i in range(10_000))
    read_artifact, artifact_id = await _stored(tmp_path, text)

    first = await read_artifact.ainvoke({"id": f"artifact://{artifact_id}", "limit": 21})
    second = await read_artifact.ainvoke({"id": artifact_id, "offset": 21, "limit": 14})

    assert first == f"[bytes 0-21 of {len(text)}]\n" + text[:21]
    assert second == f"[bytes 21-35 of {len(text)}]\n" + text[21:35]


async def test_read_artifact_refuses_ids_that_are_not_ours(tmp_path: Path) -> None:
    read_artifact, _ = await _stored(tmp_path, "w" * 30_000)

    for bad in ("../../etc/passwd", "/etc/passwd", "..", "not-a-uuid", ""):
        assert "not a valid artifact id" in await read_artifact.ainvoke({"id": bad})
    missing = await read_artifact.ainvoke({"id": "0" * 32})
    assert missing.startswith("Error:") and "not found" in missing


async def test_read_artifact_clamps_a_huge_limit(tmp_path: Path) -> None:
    read_artifact, artifact_id = await _stored(tmp_path, "v" * 500_000)

    piece = await read_artifact.ainvoke({"id": artifact_id, "limit": 10_000_000})

    assert len(piece) < 90_000  # it cannot be used to flood the context again
