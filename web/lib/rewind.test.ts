import assert from "node:assert/strict";
import { test } from "node:test";
import type { HistoryRow, TurnInfo } from "./api.ts";
import { rewindTargets } from "./rewind.ts";

const user = (text: string): HistoryRow => ({ kind: "user", text, args: null, result: null });
const reply = (text: string): HistoryRow => ({ kind: "assistant", text, args: null, result: null });
const tool = (): HistoryRow => ({ kind: "tool", text: "write_file", args: {}, result: "ok" });
const turn = (user_text: string, id: string, owner = "s1"): TurnInfo => ({
  owner_session_id: owner,
  started_event_id: id,
  completed_event_id: null,
  timestamp: "2026-11-27T10:00:00+00:00",
  user_text,
  files_changed: 0,
});
const ids = (m: Map<number, TurnInfo>) => [...m].map(([i, t]) => `${i}:${t.started_event_id}`);

test("each user message maps to its turn, in order", () => {
  const rows = [user("one"), tool(), reply("a"), user("two"), reply("b"), user("three"), reply("c")];
  const turns = [turn("one", "e1"), turn("two", "e2"), turn("three", "e3")];
  assert.deepEqual(ids(rewindTargets(rows, turns)), ["5:e3", "3:e2", "0:e1"]);
});

test("the same text twice maps to the right occurrence", () => {
  const rows = [user("go"), reply("a"), user("go"), reply("b")];
  const turns = [turn("go", "e1"), turn("go", "e2")];
  assert.deepEqual(ids(rewindTargets(rows, turns)), ["2:e2", "0:e1"]);
});

test("turns across a fork keep their owner conversation", () => {
  const rows = [user("parent turn"), reply("a"), user("fork turn"), reply("b")];
  const turns = [turn("parent turn", "p1", "parent"), turn("fork turn", "f1", "fork")];
  const got = rewindTargets(rows, turns);
  assert.equal(got.get(0)?.owner_session_id, "parent");
  assert.equal(got.get(2)?.owner_session_id, "fork");
});

test("a queued message read mid-run has no turn and gets no rewind", () => {
  const rows = [user("build it"), tool(), user("also add tests"), reply("done")];
  const turns = [turn("build it", "e1")];
  assert.deepEqual(ids(rewindTargets(rows, turns)), ["0:e1"]);
  // even when the queued text is part of the turn's text
  assert.deepEqual(ids(rewindTargets([user("build it"), user("it")], turns)), ["0:e1"]);
});

test("compacted turns are skipped; whitespace and a stripped skill body still match", () => {
  const rows: HistoryRow[] = [{ kind: "compaction", text: "summary", args: null, result: null }, user("  fix   it "), reply("ok")];
  const turns = [turn("old", "e1"), turn("fix it\n\n<skill-invocation name=\"x\">\nbody\n</skill-invocation>", "e2")];
  assert.deepEqual(ids(rewindTargets(rows, turns)), ["1:e2"]);
});

test("nothing to map", () => {
  assert.equal(rewindTargets([], []).size, 0);
  assert.equal(rewindTargets([user("hi")], []).size, 0);
});
