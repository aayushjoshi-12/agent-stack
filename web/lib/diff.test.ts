import assert from "node:assert/strict";
import { test } from "node:test";
import { isMarkdownPath, isTruncated, parseUnifiedDiff, previewEdit, previewWrite } from "./diff.ts";

const DIFF = ["--- a/a.py", "+++ b/a.py", "@@ -1,3 +1,4 @@", " x = 1", "-y = 2", "+y = 3", "+z = 4", " w = 5"].join("\n");

test("a unified diff becomes rows with correct old and new line numbers", () => {
  const rows = parseUnifiedDiff(DIFF);
  assert.deepEqual(rows.map((r) => r.kind), ["meta", "meta", "hunk", "ctx", "del", "add", "add", "ctx"]);
  assert.deepEqual(rows[3], { kind: "ctx", text: "x = 1", oldNo: 1, newNo: 1 });
  assert.deepEqual(rows[4], { kind: "del", text: "y = 2", oldNo: 2 });
  assert.deepEqual(rows[5], { kind: "add", text: "y = 3", newNo: 2 });
  assert.deepEqual(rows[6], { kind: "add", text: "z = 4", newNo: 3 });
  assert.deepEqual(rows[7], { kind: "ctx", text: "w = 5", oldNo: 3, newNo: 4 });
});

test("a second hunk restarts the numbering; a line that merely starts with dashes is content", () => {
  const rows = parseUnifiedDiff(["--- a/f", "+++ b/f", "@@ -1 +1 @@", "-a", "+b", "@@ -10 +10 @@", "--- not a header", "++ also content"].join("\n"));
  assert.deepEqual(rows[rows.length - 2], { kind: "del", text: "-- not a header", oldNo: 10 });
  assert.deepEqual(rows[rows.length - 1], { kind: "add", text: "+ also content", newNo: 10 });
});

test("an empty diff and a new-file diff parse sanely", () => {
  assert.deepEqual(parseUnifiedDiff(""), []);
  const rows = parseUnifiedDiff("--- /dev/null\n+++ b/n.txt\n@@ -0,0 +1,2 @@\n+a\n+b");
  assert.deepEqual(rows.slice(-2), [{ kind: "add", text: "a", newNo: 1 }, { kind: "add", text: "b", newNo: 2 }]);
});

test("the no-newline marker is not a content line", () => {
  const rows = parseUnifiedDiff("--- a/f\n+++ b/f\n@@ -1 +1 @@\n-a\n\\ No newline at end of file\n+b");
  assert.equal(rows.filter((r) => r.kind === "meta").length, 3);
});

test("an edit preview shows removed lines then added lines", () => {
  const rows = previewEdit("old1\nold2\n", "new1\n");
  assert.deepEqual(rows.map((r) => r.kind), ["del", "del", "add"]);
  assert.deepEqual(previewEdit("", "x"), [{ kind: "add", text: "x", newNo: 1 }]);
});

test("a write preview is capped and says how much it left out", () => {
  const { rows, hidden } = previewWrite(Array.from({ length: 100 }, (_, i) => `l${i}`).join("\n"), 40);
  assert.equal(rows.length, 40);
  assert.equal(hidden, 60);
  assert.deepEqual(previewWrite("a\nb"), { rows: [{ kind: "add", text: "a", newNo: 1 }, { kind: "add", text: "b", newNo: 2 }], hidden: 0 });
});

test("truncation marker and markdown path detection", () => {
  assert.equal(isTruncated("abc...[truncated 912 bytes]"), true);
  assert.equal(isTruncated("abc"), false);
  assert.deepEqual(["a.md", "docs/B.MARKDOWN", "c.mdx", "d.py", "readme"].map(isMarkdownPath), [true, true, true, false, false]);
});
