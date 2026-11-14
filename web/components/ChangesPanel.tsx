"use client";

import { useEffect, useMemo, useState } from "react";
import { api, type ChangedFile, type ChangedFileDetail, type Conn } from "@/lib/api";
import { isMarkdownPath, parseUnifiedDiff, type DiffRow } from "@/lib/diff";
import { DiffView } from "./DiffView";
import { Markdown } from "./Markdown";

type Tab = "diff" | "file" | "rendered";
const MAX_FILE_ROWS = 3000;

function fileRows(text: string): { rows: DiffRow[]; hidden: number } {
  const lines = text.replace(/\n$/, "").split("\n");
  const rows: DiffRow[] = lines.slice(0, MAX_FILE_ROWS).map((t, i) => ({ kind: "ctx", text: t, oldNo: i + 1, newNo: i + 1 }));
  return { rows, hidden: Math.max(lines.length - MAX_FILE_ROWS, 0) };
}

/**
 * Every file the agent created or edited in this conversation: pick one for its diff, its final
 * text, or (for markdown) the rendered page. Only changes made through the agent's file tools are
 * known, which the footer says.
 */
export function ChangesPanel({
  conn,
  sessionId,
  files,
  note,
  focusPath,
  refreshKey,
  onClose,
}: {
  conn: Conn;
  sessionId: string;
  files: ChangedFile[];
  note: string;
  focusPath: string | null;
  refreshKey: number;
  onClose: () => void;
}) {
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<ChangedFileDetail | null>(null);
  const [tab, setTab] = useState<Tab>("diff");
  const [error, setError] = useState("");

  const path = selected && files.some((f) => f.path === selected) ? selected : (files[0]?.path ?? null);

  useEffect(() => {
    if (focusPath && files.some((f) => f.path === focusPath)) setSelected(focusPath);
  }, [focusPath, files]);

  useEffect(() => {
    if (!path) {
      setDetail(null);
      return;
    }
    let current = true;
    setError("");
    api
      .changeFile(conn, sessionId, path)
      .then((d) => current && setDetail(d))
      .catch((e: unknown) => current && setError(e instanceof Error ? e.message : "Could not load that file."));
    return () => {
      current = false;
    };
  }, [conn, sessionId, path, refreshKey]);

  const diffRows = useMemo(() => (detail ? parseUnifiedDiff(detail.diff) : []), [detail]);
  const text = useMemo(() => (detail ? fileRows(detail.after) : { rows: [], hidden: 0 }), [detail]);
  const markdown = path !== null && isMarkdownPath(path);
  const shownTab: Tab = tab === "rendered" && !markdown ? "diff" : tab;

  return (
    <aside className="flex h-full min-h-0 flex-col border-l-2 border-line bg-surface" aria-label="Files the agent changed">
      <header className="flex items-center gap-3 border-b-2 border-line px-4 py-3">
        <h2 className="flex-1 text-lg font-bold">
          Files changed <span className="code text-soft">{files.length}</span>
        </h2>
        <button type="button" className="btn" onClick={onClose} aria-label="Close the files panel">
          ✕
        </button>
      </header>

      {files.length === 0 ? (
        <p className="p-4 text-soft">The agent has not created or edited any files in this conversation yet.</p>
      ) : (
        <>
          <ul className="contain max-h-48 shrink-0 space-y-2 overflow-y-auto border-b-2 border-line p-3">
            {files.map((f) => (
              <li key={f.path}>
                <button
                  type="button"
                  onClick={() => setSelected(f.path)}
                  aria-current={f.path === path ? "true" : undefined}
                  className={`flex w-full items-center gap-2 border-2 border-line px-3 py-1.5 text-left hover:bg-sunk ${f.path === path ? "bg-blue text-on-accent hover:bg-blue" : "bg-paper"}`}
                >
                  <span className="code shrink-0 text-xs font-bold uppercase">{f.kind === "created" ? "new" : "edit"}</span>
                  <span className="code min-w-0 flex-1 truncate" title={f.path}>
                    {f.path}
                  </span>
                  <span className="code shrink-0 tabular-nums">
                    +{f.added} −{f.removed}
                    {f.edits > 1 ? ` ×${f.edits}` : ""}
                  </span>
                </button>
              </li>
            ))}
          </ul>

          <div className="flex gap-2 border-b-2 border-line px-3 py-2" role="tablist" aria-label="View">
            {(["diff", "file", ...(markdown ? (["rendered"] as const) : [])] as Tab[]).map((t) => (
              <button
                key={t}
                type="button"
                role="tab"
                aria-selected={shownTab === t}
                onClick={() => setTab(t)}
                className={`btn ${shownTab === t ? "btn-yellow" : "btn-ghost"}`}
              >
                {t === "diff" ? "Diff" : t === "file" ? "File" : "Rendered"}
              </button>
            ))}
          </div>

          <div className="contain min-h-0 flex-1 overflow-y-auto">
            {error && (
              <p role="alert" className="p-4 font-bold text-red">
                {error}
              </p>
            )}
            {!error && !detail && <p className="code p-4 text-soft">loading…</p>}
            {detail && shownTab === "diff" && (
              <>
                {detail.kind === "created" && <p className="border-b-2 border-line px-3 py-1 text-sm text-soft">New file: every line is an addition.</p>}
                {detail.diff === "" ? (
                  <p className="code p-4 text-soft">No net difference: the edits cancelled each other out.</p>
                ) : (
                  <DiffView rows={diffRows} label={`Diff of ${detail.path}`} />
                )}
                {detail.diff_truncated && <p className="code p-3 text-soft">… the diff was cut at 4000 lines. Use the File tab for the whole text.</p>}
              </>
            )}
            {detail && shownTab === "file" && (
              <>
                <DiffView rows={text.rows} label={`Contents of ${detail.path}`} />
                {text.hidden > 0 && <p className="code p-3 text-soft">… {text.hidden.toLocaleString()} more lines not shown.</p>}
              </>
            )}
            {detail && shownTab === "rendered" && (
              <div className="p-4">
                <Markdown text={detail.after} />
              </div>
            )}
          </div>
        </>
      )}
      <footer className="border-t-2 border-line px-4 py-2 text-xs text-soft">{note}</footer>
    </aside>
  );
}
