"""P32: ranked full-text search over real transcripts (SQLite FTS5 + BM25)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_stack.persistence.conversation import FileConversationStore
from agent_stack.persistence.search import ChatSearchIndex, match_query
from agent_stack.persistence.sqlite import SqliteDatabase


def _index(tmp_path: Path) -> tuple[ChatSearchIndex, FileConversationStore]:
    store = FileConversationStore(tmp_path / "conv")
    return ChatSearchIndex(SqliteDatabase(tmp_path / "index.db"), store), store


async def _three_chats(store: FileConversationStore) -> list[tuple[str, str]]:
    await store.append_messages(
        "discussed",
        [
            HumanMessage(content="should we store embeddings in pgvector?"),
            AIMessage(content="pgvector keeps embeddings next to the rows; I would use pgvector."),
        ],
    )
    call = {"name": "run_command", "args": {"command": "pip list"}, "id": "c1"}
    await store.append_messages(
        "scrolled",
        [
            HumanMessage(content="list the installed packages"),
            AIMessage(content="", tool_calls=[call]),
            ToolMessage(content="numpy 2.1\npgvector 0.3\nrequests 2.32", tool_call_id="c1"),
        ],
    )
    await store.append_messages("other", [HumanMessage(content="write a haiku about rain")])
    return [("discussed", "Vector store"), ("scrolled", "Packages"), ("other", "Poem")]


async def test_talked_about_beats_scrolled_past_and_unrelated_chats_are_left_out(
    tmp_path: Path,
) -> None:
    index, store = _index(tmp_path)
    sessions = await _three_chats(store)

    hits = await index.search(sessions, "pgvector", limit=5)

    assert [h.session_id for h in hits] == ["discussed", "scrolled"]
    assert "[pgvector]" in hits[0].snippets[0]
    assert len(hits[0].snippets) == 2  # both messages that mention it


async def test_stems_titles_phrases_and_operator_words_all_work(tmp_path: Path) -> None:
    index, store = _index(tmp_path)
    sessions = await _three_chats(store)

    assert [h.session_id for h in await index.search(sessions, "storing")] == ["discussed"]
    assert [h.session_id for h in await index.search(sessions, "poem")] == ["other"]  # title
    assert (await index.search(sessions, "poem"))[0].seq == -1
    phrase = await index.search(sessions, '"installed packages"')
    assert [h.session_id for h in phrase] == ["scrolled"]
    # FTS5 syntax in a query is just words, never an error
    for odd in ('AND OR NOT "', "NEAR(a b)", "col:value*", "^", ""):
        await index.search(sessions, odd)
    assert match_query('pip "two words" AND') == '"pip" OR "two words" OR "AND"'


async def test_changed_chats_are_reindexed_and_unchanged_ones_are_not_read_again(
    tmp_path: Path,
) -> None:
    index, store = _index(tmp_path)
    sessions = await _three_chats(store)
    reads: list[str] = []
    real_read = store.read

    async def counting_read(session_id: str) -> Any:
        reads.append(session_id)
        return await real_read(session_id)

    store.read = counting_read  # type: ignore[method-assign]

    await index.search(sessions, "rain")
    assert sorted(reads) == ["discussed", "other", "scrolled"]  # first search builds the index
    reads.clear()
    await index.search(sessions, "rain")
    assert reads == []  # nothing changed: nothing read

    await store.append_messages("other", [AIMessage(content="drizzle on the roof")])
    renamed = [(sid, "Weather" if sid == "scrolled" else title) for sid, title in sessions]
    assert [h.session_id for h in await index.search(renamed, "drizzle")] == ["other"]
    assert sorted(reads) == ["other", "scrolled"]  # only the appended and the renamed chat
    assert [h.session_id for h in await index.search(renamed, "weather")] == ["scrolled"]
    assert await index.search(renamed, "packages") != []  # its old text is still there


async def test_search_only_sees_the_sessions_it_is_given(tmp_path: Path) -> None:
    index, store = _index(tmp_path)
    sessions = await _three_chats(store)
    await index.search(sessions, "pgvector")  # all three indexed

    mine = [s for s in sessions if s[0] != "discussed"]  # another owner's chat, or a deleted one
    assert [h.session_id for h in await index.search(mine, "pgvector")] == ["scrolled"]
