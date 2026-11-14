import assert from "node:assert/strict";
import { test } from "node:test";
import { parseSse } from "./api.ts";

const block = (seq: number, type: string) =>
  `id: ${seq}\nevent: ${type}\ndata: ${JSON.stringify({ id: `e${seq}`, type, sequence: seq })}\n\n`;

test("complete blocks become events and a partial block is kept for the next chunk", () => {
  const text = block(0, "run.started") + block(1, "assistant.message") + "id: 2\nevent: run.comp";
  const { events, rest } = parseSse(text);
  assert.deepEqual(events.map((e) => e.sequence), [0, 1]);
  assert.equal(rest, "id: 2\nevent: run.comp");
});

test("keep-alive comments and malformed blocks are skipped, not fatal", () => {
  const text = `: keepalive\n\ndata: {not json\n\n${block(3, "run.completed")}`;
  const { events } = parseSse(text);
  assert.deepEqual(events.map((e) => e.sequence), [3]);
});
