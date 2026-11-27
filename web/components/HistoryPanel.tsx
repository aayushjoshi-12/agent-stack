"use client";

import { useCallback, useEffect, useState } from "react";
import { api, type Conn, type RewindResult, type TurnInfo } from "@/lib/api";
import { Mermaid } from "./Mermaid";

export type HistoryTab = "rewind" | "branches" | "share";

export const MODES = [
  { key: "both", label: "Restore code and conversation", conversation: true, code: true },
  { key: "conversation", label: "Restore conversation (keep the code as it is now)", conversation: true, code: false },
  { key: "code", label: "Restore code (keep the conversation)", conversation: false, code: true },
] as const;

/**
 * Go back, see the branches, share a point. Opened with Esc Esc or the History button.
 * Nothing here deletes: a rewind forks, so the conversation you leave stays in the list.
 */
export function HistoryPanel({
  conn,
  sessionId,
  initialTab,
  onClose,
  onRewound,
  onOpened,
  onError,
}: {
  conn: Conn;
  sessionId: string;
  initialTab: HistoryTab;
  onClose: () => void;
  onRewound: (result: RewindResult) => void;
  onOpened: (id: string) => void;
  onError: (e: unknown) => void;
}) {
  const [tab, setTab] = useState<HistoryTab>(initialTab);
  const [turns, setTurns] = useState<TurnInfo[] | null>(null);
  const [picked, setPicked] = useState<TurnInfo | null>(null);
  const [tree, setTree] = useState<{ text: string; mermaid: string } | null>(null);
  const [reference, setReference] = useState("");
  const [incoming, setIncoming] = useState("");
  const [preview, setPreview] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      if (picked) setPicked(null);
      else onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [picked, onClose]);

  useEffect(() => {
    let current = true;
    if (tab === "rewind" && turns === null) {
      api
        .turns(conn, sessionId)
        .then((t) => current && setTurns(t.filter((x) => x.user_text)))
        .catch((e) => current && onError(e));
    }
    if (tab === "branches" && tree === null) {
      api
        .tree(conn, sessionId)
        .then((t) => current && setTree(t))
        .catch((e) => current && onError(e));
    }
    return () => {
      current = false;
    };
  }, [tab, conn, sessionId, turns, tree, onError]);

  const run = useCallback(
    async (action: () => Promise<void>) => {
      setBusy(true);
      try {
        await action();
      } catch (e) {
        onError(e);
      } finally {
        setBusy(false);
      }
    },
    [onError],
  );

  const rewind = (turn: TurnInfo, mode: (typeof MODES)[number]) =>
    run(async () => {
      const result = await api.rewind(conn, sessionId, {
        owner_session_id: turn.owner_session_id,
        started_event_id: turn.started_event_id,
        conversation: mode.conversation,
        code: mode.code,
      });
      onRewound(result);
    });

  const tabs: { key: HistoryTab; label: string }[] = [
    { key: "rewind", label: "Rewind" },
    { key: "branches", label: "Branches" },
    { key: "share", label: "Share a point" },
  ];

  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-ink/50 p-4 sm:p-10" role="dialog" aria-modal="true" aria-label="History">
      <div className="box w-full max-w-2xl bg-surface p-4">
        <div className="mb-3 flex items-center gap-2">
          <div role="tablist" className="flex flex-1 gap-2">
            {tabs.map((t) => (
              <button key={t.key} role="tab" aria-selected={tab === t.key} className={`btn ${tab === t.key ? "btn-yellow" : ""}`} onClick={() => { setTab(t.key); setPicked(null); }}>
                {t.label}
              </button>
            ))}
          </div>
          <button className="btn btn-ghost" onClick={onClose} aria-label="Close history">
            Close
          </button>
        </div>

        {note && <p className="code mb-2 text-soft">{note}</p>}

        {tab === "rewind" && (
          <div>
            {turns === null && <p className="code text-soft">Loading…</p>}
            {turns?.length === 0 && <p className="code text-soft">Nothing to rewind to yet.</p>}
            {!picked && turns && turns.length > 0 && (
              <>
                <p className="mb-2 font-bold">Go back to just before…</p>
                <ul className="space-y-2">
                  {[...turns].reverse().map((t) => (
                    <li key={t.started_event_id}>
                      <button className="btn w-full justify-between text-left" onClick={() => setPicked(t)}>
                        <span className="min-w-0 flex-1 truncate">{t.user_text.trim().split("\n")[0]}</span>
                        {t.files_changed > 0 && <span className="code text-soft">{t.files_changed} file{t.files_changed === 1 ? "" : "s"}</span>}
                      </button>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {picked && (
              <div className="space-y-2">
                <p className="font-bold">Rewind to before: “{picked.user_text.trim().slice(0, 80)}”</p>
                {MODES.map((m) => (
                  <button key={m.key} className="btn w-full text-left" disabled={busy} onClick={() => void rewind(picked, m)}>
                    {m.label}
                  </button>
                ))}
                <button className="btn btn-ghost" onClick={() => setPicked(null)}>
                  Back
                </button>
              </div>
            )}
          </div>
        )}

        {tab === "branches" && (
          <div className="space-y-3">
            {tree === null && <p className="code text-soft">Loading…</p>}
            {tree && (
              <>
                <Mermaid chart={tree.mermaid} />
                <pre className="code overflow-x-auto border-2 border-line bg-sunk p-2 text-sm">{tree.text}</pre>
              </>
            )}
          </div>
        )}

        {tab === "share" && (
          <div className="space-y-4">
            <div className="space-y-2">
              <p className="font-bold">Name this point</p>
              <button
                className="btn"
                disabled={busy}
                onClick={() =>
                  void run(async () => {
                    setReference((await api.makeReference(conn, sessionId)).reference);
                  })
                }
              >
                Make a reference to the last finished turn
              </button>
              {reference && (
                <div className="flex items-center gap-2">
                  <input readOnly value={reference} className="code min-w-0 flex-1 border-2 border-line bg-sunk p-2" aria-label="Reference" onFocus={(e) => e.currentTarget.select()} />
                  <button className="btn" onClick={() => void navigator.clipboard?.writeText(reference).then(() => setNote("Copied."), () => setNote("Select the text and copy it."))}>
                    Copy
                  </button>
                </div>
              )}
            </div>
            <div className="space-y-2">
              <p className="font-bold">Open one from another chat</p>
              <input value={incoming} onChange={(e) => { setIncoming(e.target.value); setPreview(""); }} placeholder="asref://…" className="code w-full border-2 border-line bg-sunk p-2" aria-label="Paste a reference" />
              <div className="flex gap-2">
                <button className="btn" disabled={busy || !incoming.trim()} onClick={() => void run(async () => setPreview((await api.describeReference(conn, incoming.trim())).text))}>
                  Preview
                </button>
                <button className="btn btn-blue" disabled={busy || !incoming.trim()} onClick={() => void run(async () => onOpened((await api.openReference(conn, incoming.trim())).id))}>
                  Open as a new conversation
                </button>
              </div>
              {preview && <pre className="code max-h-72 overflow-auto whitespace-pre-wrap border-2 border-line bg-sunk p-2 text-sm">{preview}</pre>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
