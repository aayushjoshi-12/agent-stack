import assert from "node:assert/strict";
import { test } from "node:test";
import { formatCost, formatTokens, formatWhen } from "./format.ts";

test("costs keep cheap models readable", () => {
  assert.deepEqual([0, 0.00042, 0.0123, 1.234, 12].map(formatCost), ["$0.00", "$0.0004", "$0.012", "$1.23", "$12.00"]);
});

test("token counts are compact", () => {
  assert.deepEqual([950, 12300, 1000, 2500000].map(formatTokens), ["950", "12.3k", "1k", "2.5M"]);
});

test("formatWhen says today, yesterday, or the date", () => {
  const now = new Date(2026, 10, 25, 15, 0);
  assert.equal(formatWhen(new Date(2026, 10, 25, 9, 5).toISOString(), now), "today 09:05");
  assert.equal(formatWhen(new Date(2026, 10, 24, 22, 30).toISOString(), now), "yesterday 22:30");
  assert.match(formatWhen(new Date(2026, 10, 14, 7, 21).toISOString(), now), /^14 Nov 07:21$/);
  assert.match(formatWhen(new Date(2025, 9, 21, 7, 21).toISOString(), now), /2025 07:21$/);
  assert.equal(formatWhen(null), "");
  assert.equal(formatWhen("garbage"), "");
});
