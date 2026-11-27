import type { HistoryRow, TurnInfo } from "./api.ts";

// A skill's body is not chat: the server strips it from history rows (application.py), so here too.
const SKILL_BLOCK = /\n*<skill-invocation [\s\S]*?<\/skill-invocation>/g;
const norm = (s: string) => s.replace(SKILL_BLOCK, "").replace(/\s+/g, " ").trim();

/**
 * Which turn each user message in the chat rewinds to, by row index.
 *
 * History rows and turns come from the same stored messages, so they line up from the end; but
 * a message queued into a working run has a row and no turn, and compaction drops rows whose
 * turns remain. So both are walked backwards and paired by text. Unpaired rows get no rewind.
 */
export function rewindTargets(rows: HistoryRow[], turns: TurnInfo[]): Map<number, TurnInfo> {
  const found = new Map<number, TurnInfo>();
  let next = turns.length - 1;
  for (let i = rows.length - 1; i >= 0 && next >= 0; i -= 1) {
    const row = rows[i];
    if (row?.kind !== "user") continue;
    const text = norm(row.text);
    if (!text) continue;
    for (let k = next; k >= 0; k -= 1) {
      const said = norm(turns[k]?.user_text ?? "");
      if (said === text) {
        found.set(i, turns[k] as TurnInfo);
        next = k - 1;
        break;
      }
    }
  }
  return found;
}
