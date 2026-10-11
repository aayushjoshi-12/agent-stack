"""Unit tests for :mod:`agent_stack.artifacts`."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_stack.artifacts import ArtifactRef, LocalArtifactStore, guess_mime_type
from agent_stack.core.errors import ArtifactError

PAYLOAD = b"\x00\x01binary\xffpayload"


async def test_put_get_round_trip(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    ref = await store.put(data=PAYLOAD, name="blob.bin", mime_type="application/octet-stream")

    assert await store.get(ref) == PAYLOAD
    assert ref.size_bytes == len(PAYLOAD)
    assert ref.uri == f"artifact://{ref.id}"
    assert ref.uri.startswith("artifact://")


async def test_put_text_stores_utf8(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    text = "héllo, artifact — ünïcode ☃"
    ref = await store.put_text(text=text, name="notes.txt")

    assert ref.mime_type == "text/plain"
    assert ref.size_bytes == len(text.encode("utf-8"))
    assert (await store.get(ref)).decode("utf-8") == text


async def test_two_puts_same_name_get_distinct_ids(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    first = await store.put(data=b"first", name="same.txt")
    second = await store.put(data=b"second", name="same.txt")

    assert first.id != second.id
    # Both payloads survive: no overwrite.
    assert await store.get(first) == b"first"
    assert await store.get(second) == b"second"
    stored = {path.name for path in tmp_path.iterdir()}
    assert {first.id, second.id} <= stored


async def test_malicious_name_does_not_escape_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    store = LocalArtifactStore(root)

    ref = await store.put(data=b"evil", name="../evil.txt")

    # The name is sanitized and the id — not the name — is authoritative.
    assert ref.name == "evil.txt"
    assert "/" not in ref.id
    stored_root = root.resolve()
    for path in stored_root.iterdir():
        assert path.resolve().is_relative_to(stored_root)
    # Nothing was written into the parent of root.
    assert not (tmp_path / "evil.txt").exists()


async def test_get_unknown_ref_raises_artifact_error(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    missing = ArtifactRef(
        id="does-not-exist",
        name="missing.txt",
        mime_type="text/plain",
        size_bytes=0,
        uri="artifact://does-not-exist",
    )

    with pytest.raises(ArtifactError):
        await store.get(missing)


async def test_delete_removes_file(tmp_path: Path) -> None:
    store = LocalArtifactStore(tmp_path)
    ref = await store.put(data=b"temporary", name="temp.log")

    assert (tmp_path / ref.id).exists()
    await store.delete(ref)
    assert not (tmp_path / ref.id).exists()

    # Deleting a missing artifact is a documented no-op (idempotent).
    await store.delete(ref)


def test_guess_mime_type_common_extensions() -> None:
    assert guess_mime_type("a.txt") == "text/plain"
    assert guess_mime_type("a.md") == "text/markdown"
    assert guess_mime_type("a.json") == "application/json"
    assert guess_mime_type("a.log") == "text/plain"
    assert guess_mime_type("a.bin") == "application/octet-stream"
