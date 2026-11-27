import assert from "node:assert/strict";
import { test } from "node:test";
import { applyEvent, emptyLive, OUTPUT_CAP, toolLabel, type ApiEvent, type LiveState } from "./events.ts";

let n = 0;
const seq: Record<string, number> = {};
function ev(type: string, data: Record<string, unknown> = {}, run = "r1", parent: string | null = null): ApiEvent {
  seq[run] = (seq[run] ?? -1) + 1;
  n += 1;
  return { id: `e${n}`, type, run_id: run, session_id: "s", sequence: seq[run] as number, timestamp: "", parent_run_id: parent, data };
}
const play = (events: ApiEvent[], from: LiveState = emptyLive()) => events.reduce(applyEvent, from);

test("a tool call goes from running to ok, and its output accumulates", () => {
  const s = play([
    ev("run.started"),
    ev("tool.started", { tool: "run_command", tool_call_id: "c1", args: { command: "ls" } }),
    ev("tool.output", { tool_call_id: "c1", chunk: "a\n" }),
    ev("tool.output", { tool_call_id: "c1", chunk: "b\n" }),
    ev("tool.completed", { tool_call_id: "c1", ok: true }),
    ev("assistant.message", { content: "done" }),
    ev("run.completed"),
  ]);
  const tool = s.items.find((i) => i.kind === "tool");
  assert.ok(tool && tool.kind === "tool");
  assert.equal(tool.status, "ok");
  assert.equal(tool.output, "a\nb\n");
  assert.equal(s.phase, "completed");
  assert.ok(s.completedEventId);
});

test("a failed tool keeps its error", () => {
  const s = play([
    ev("tool.started", { tool: "t", tool_call_id: "c", args: {} }),
    ev("tool.completed", { tool_call_id: "c", ok: false, error: "boom" }),
  ]);
  const tool = s.items[0];
  assert.ok(tool && tool.kind === "tool");
  assert.deepEqual([tool.status, tool.error], ["failed", "boom"]);
});

test("duplicate and replayed events are applied once", () => {
  const first = ev("assistant.message", { content: "hi" });
  const once = play([first]);
  const twice = applyEvent(once, first);
  assert.equal(twice, once);
  assert.equal(play([ev("run.started")], once).items.length, 1);
});

test("output is capped so a runaway command cannot grow the page without bound", () => {
  const big = "x".repeat(OUTPUT_CAP);
  const s = play([
    ev("tool.started", { tool: "run_command", tool_call_id: "c", args: {} }),
    ev("tool.output", { tool_call_id: "c", chunk: big }),
    ev("tool.output", { tool_call_id: "c", chunk: "more" }),
  ]);
  const tool = s.items[0];
  assert.ok(tool && tool.kind === "tool");
  assert.equal(tool.output.length, OUTPUT_CAP);
  assert.equal(tool.truncated, true);
});

test("an approval attaches to its tool call and resolves", () => {
  let s = play([
    ev("tool.started", { tool: "write_file", tool_call_id: "c", args: { path: "x" } }),
    ev("approval.required", { approval_id: "a1", tool_call_id: "c", tool: "write_file", args: { path: "x" } }),
  ]);
  const pending = s.items[0];
  assert.ok(pending && pending.kind === "tool");
  assert.deepEqual(pending.approval, { id: "a1", state: "pending" });
  s = play([ev("approval.resolved", { approval_id: "a1", tool_call_id: "c", status: "rejected" })], s);
  const done = s.items[0];
  assert.ok(done && done.kind === "tool");
  assert.equal(done.approval?.state, "rejected");
});

test("subagents nest their events and report a final status", () => {
  const s = play([
    ev("run.started"),
    ev("subagent.started", { child_run_id: "k1", agent_name: "explorer" }),
    ev("tool.started", { tool: "read_file", tool_call_id: "x", args: { path: "a" } }, "k1", "r1"),
    ev("subagent.completed", { child_run_id: "k1", status: "completed", steps: 2, tool_calls: 1 }),
  ]);
  const child = s.items.find((i) => i.kind === "tool");
  const card = s.items.find((i) => i.kind === "subagent");
  assert.ok(child && child.depth === 1);
  assert.ok(card && card.kind === "subagent");
  assert.deepEqual([card.status, card.steps, card.toolCalls], ["completed", 2, 1]);
  assert.equal(s.phase, "running"); // a child's events never end the parent's run
});

test("cancelled runs are not shown as failures; real failures are", () => {
  const cancelled = play([ev("run.started"), ev("run.failed", { error: "RunCancelled('x')" })]);
  assert.equal(cancelled.phase, "cancelled");
  assert.equal(cancelled.items.length, 0);
  const failed = play([ev("run.started"), ev("run.failed", { error: "RuntimeError('boom')" })]);
  assert.equal(failed.phase, "failed");
  assert.equal(failed.items[0]?.kind, "failure");
});

test("tool labels prefer the command, then a short string argument", () => {
  assert.equal(toolLabel("run_command", { command: "pytest -q" }), "run_command pytest -q");
  assert.equal(toolLabel("read_file", { path: "a.py", n: 3 }), "read_file a.py");
  assert.equal(toolLabel("x", {}), "x");
  assert.ok(toolLabel("t", { command: "y".repeat(200) }).length <= 72);
});

test("each model.usage event nudges the cost display, from any run, and never repeats", () => {
  const started = ev("run.started");
  const first = ev("model.usage", { cost_usd: 0.001 });
  let s = play([started, first, ev("model.usage", { cost_usd: 0.002 }, "k1", "r1")]);
  assert.equal(s.usageTick, 2);
  s = applyEvent(s, first); // a replayed duplicate
  assert.equal(s.usageTick, 2);
  assert.equal(s.items.length, 0); // usage is not a transcript row
});

test("a finished write or edit nudges the changed-files list; a failed one or a read does not", () => {
  const s = play([
    ev("run.started"),
    ev("tool.started", { tool: "write_file", tool_call_id: "w", args: { path: "a" } }),
    ev("tool.completed", { tool: "write_file", tool_call_id: "w", ok: true }),
    ev("tool.started", { tool: "edit_file", tool_call_id: "e", args: { path: "a" } }),
    ev("tool.completed", { tool: "edit_file", tool_call_id: "e", ok: false, error: "not found" }),
    ev("tool.started", { tool: "read_file", tool_call_id: "r", args: { path: "a" } }),
    ev("tool.completed", { tool: "read_file", tool_call_id: "r", ok: true }),
  ]);
  assert.equal(s.changeTick, 1);
});

test("auto mode's verdict and its reason land on the tool, and an ask carries its note", () => {
  const s = play([
    ev("run.started"),
    ev("tool.started", { tool: "run_command", tool_call_id: "c1", args: { command: "ls" } }),
    ev("approval.auto", { tool: "run_command", tool_call_id: "c1", verdict: "allow", reason: "read-only" }),
    ev("tool.started", { tool: "run_command", tool_call_id: "c2", args: { command: "git push" } }),
    ev("approval.auto", { tool: "run_command", tool_call_id: "c2", verdict: "ask", reason: "it pushes code" }),
    ev("approval.required", { approval_id: "a1", tool_call_id: "c2", tool: "run_command", args: {}, note: "it pushes code" }),
  ]);
  const [one, two] = s.items.filter((i) => i.kind === "tool");
  assert.ok(one && one.kind === "tool" && two && two.kind === "tool");
  assert.deepEqual(one.auto, { verdict: "allow", reason: "read-only" });
  assert.deepEqual(two.approval, { id: "a1", state: "pending", note: "it pushes code" });
});

test("an auto-compaction shows as a notice, a subagent's does not", () => {
  const s = play([
    ev("run.started"),
    ev("context.compacted", { messages: 31, auto: true }),
    ev("context.compacted", { messages: 9, auto: true }, "child", "r1"),
  ]);
  const notes = s.items.filter((i) => i.kind === "notice");
  assert.equal(notes.length, 1);
  assert.match(notes[0]?.text ?? "", /31 messages summarised/);
});

const delta = (text: string, step: number, kind = "text", run = "r1", parent: string | null = null): ApiEvent => ({
  id: `d${(n += 1)}`,
  type: "assistant.delta",
  run_id: run,
  session_id: "s",
  sequence: -1,
  timestamp: "",
  parent_run_id: parent,
  data: { text, kind, step },
});

const shape = (s: LiveState) =>
  s.items.map((i) => [i.kind, "text" in i ? i.text : "", "streaming" in i ? i.streaming : undefined]);

test("deltas grow one streaming bubble and the final message replaces it in place", () => {
  const started = play([ev("run.started")]);
  const streaming = play([delta("thin", 0, "reasoning"), delta("king", 0, "reasoning"), delta("Hel", 0), delta("lo", 0)], started);
  assert.deepEqual(shape(streaming), [
    ["reasoning", "thinking", true],
    ["assistant", "Hello", true],
  ]);
  assert.deepEqual(streaming.cursor, started.cursor); // deltas never move the resume cursor

  const done = play([ev("model.usage", {}), ev("model.reasoning", { text: "thinking" }), ev("assistant.message", { content: "Hello" })], streaming);
  assert.deepEqual(shape(done), [
    ["reasoning", "thinking", undefined],
    ["assistant", "Hello", undefined],
  ]);
  const bubble = done.items[1];
  assert.ok(bubble && bubble.kind === "assistant" && bubble.eventId);
});

test("a step that ends in tool calls only drops its streamed bubble; the next step streams anew", () => {
  const s = play([
    ev("run.started"),
    delta(" ", 0),
    ev("assistant.message", { content: "", tool_calls: ["t"] }),
    ev("tool.started", { tool: "t", tool_call_id: "c", args: {} }),
    ev("tool.completed", { tool_call_id: "c", ok: true }),
    delta("Answer", 1),
  ]);
  assert.deepEqual(shape(s), [
    ["tool", "", undefined],
    ["assistant", "Answer", true],
  ]);
});

test("a final message the event log capped keeps the whole streamed text", () => {
  const whole = "y".repeat(2500);
  const s = play([
    ev("run.started"),
    delta(whole, 0),
    ev("assistant.message", { content: `${"y".repeat(2000)}...[truncated 500 bytes]` }),
  ]);
  assert.deepEqual(shape(s), [["assistant", whole, undefined]]);
});

test("a late delta for a finished step is dropped", () => {
  const s = play([ev("run.started"), delta("Hi", 0), ev("assistant.message", { content: "Hi" }), delta(" again", 0)]);
  assert.deepEqual(shape(s), [["assistant", "Hi", undefined]]);
});

test("a subagent's deltas never render", () => {
  const s = play([ev("run.started"), delta("child text", 0, "text", "child", "r1")]);
  assert.equal(s.items.length, 0);
  assert.equal(applyEvent(s, delta("x", 0, "text", "child", "r1")), s);
});
