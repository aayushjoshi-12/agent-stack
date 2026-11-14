import assert from "node:assert/strict";
import { test } from "node:test";
import { formatCost, formatTokens } from "./format.ts";

test("costs keep cheap models readable", () => {
  assert.deepEqual([0, 0.00042, 0.0123, 1.234, 12].map(formatCost), ["$0.00", "$0.0004", "$0.012", "$1.23", "$12.00"]);
});

test("token counts are compact", () => {
  assert.deepEqual([950, 12300, 1000, 2500000].map(formatTokens), ["950", "12.3k", "1k", "2.5M"]);
});
