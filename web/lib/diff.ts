// Turning a unified diff (or an edit that has not run yet) into rows a view can draw. Pure and
// framework-free so it is tested with `node --test`.

export type DiffRow =
  | { kind: "meta"; text: string }
  | { kind: "hunk"; text: string }
  | { kind: "add"; text: string; newNo: number }
  | { kind: "del"; text: string; oldNo: number }
  | { kind: "ctx"; text: string; oldNo: number; newNo: number };

const HUNK = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/;

/** Parse the server's unified diff into rows with old/new line numbers. */
export function parseUnifiedDiff(diff: string): DiffRow[] {
  const rows: DiffRow[] = [];
  let oldNo = 0;
  let newNo = 0;
  let inHunk = false;
  for (const line of diff.split("\n")) {
    const hunk = HUNK.exec(line);
    if (hunk) {
      oldNo = Number(hunk[1]);
      newNo = Number(hunk[2]);
      inHunk = true;
      rows.push({ kind: "hunk", text: line });
    } else if (!inHunk) {
      if (line) rows.push({ kind: "meta", text: line }); // the --- / +++ header
    } else if (line.startsWith("+")) {
      rows.push({ kind: "add", text: line.slice(1), newNo });
      newNo += 1;
    } else if (line.startsWith("-")) {
      rows.push({ kind: "del", text: line.slice(1), oldNo });
      oldNo += 1;
    } else if (line.startsWith("\\")) {
      rows.push({ kind: "meta", text: line }); // "\ No newline at end of file"
    } else {
      rows.push({ kind: "ctx", text: line.startsWith(" ") ? line.slice(1) : line, oldNo, newNo });
      oldNo += 1;
      newNo += 1;
    }
  }
  return rows;
}

/** True when the event log cut this text short (it appends `...[truncated N bytes]`). */
export function isTruncated(text: string): boolean {
  return /\.\.\.\[truncated \d+ bytes\]$/.test(text);
}

const lines = (text: string): string[] => (text === "" ? [] : text.replace(/\n$/, "").split("\n"));

/**
 * What an `edit_file` is about to do, as removed-then-added rows (no context: the file is not
 * read). Used on the approval card, before anything has changed.
 */
export function previewEdit(oldText: string, newText: string): DiffRow[] {
  const rows: DiffRow[] = [];
  lines(oldText).forEach((text, i) => rows.push({ kind: "del", text, oldNo: i + 1 }));
  lines(newText).forEach((text, i) => rows.push({ kind: "add", text, newNo: i + 1 }));
  return rows;
}

/** The first lines of a `write_file`, as additions, with how many were left out. */
export function previewWrite(content: string, limit = 40): { rows: DiffRow[]; hidden: number } {
  const all = lines(content);
  const rows: DiffRow[] = all.slice(0, limit).map((text, i) => ({ kind: "add", text, newNo: i + 1 }));
  return { rows, hidden: Math.max(all.length - limit, 0) };
}

export const isMarkdownPath = (path: string): boolean => /\.(md|markdown|mdx)$/i.test(path);
