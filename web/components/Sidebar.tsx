"use client";

import { useState } from "react";
import type { SessionSummary } from "@/lib/api";

export function Sidebar({
  sessions,
  activeId,
  busyIds,
  onSelect,
  onNew,
  onRename,
  onDelete,
  onDisconnect,
}: {
  sessions: SessionSummary[];
  activeId: string | null;
  busyIds: Set<string>;
  onSelect: (id: string) => void;
  onNew: () => void;
  onRename: (id: string, title: string) => void;
  onDelete: (id: string) => void;
  onDisconnect: () => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [confirm, setConfirm] = useState<string | null>(null);

  return (
    <nav aria-label="Conversations" className="flex h-full min-h-0 flex-col border-r-2 border-line bg-surface">
      <div className="flex items-center gap-2 border-b-2 border-line p-3">
        <span className="inline-block h-4 w-4 bg-red" aria-hidden />
        <span className="inline-block h-4 w-4 rounded-full bg-yellow" aria-hidden />
        <span className="inline-block h-4 w-4 bg-blue [clip-path:polygon(50%_0,100%_100%,0_100%)]" aria-hidden />
        <h1 className="ml-1 text-lg font-bold tracking-tight">agent-stack</h1>
      </div>
      <div className="p-3">
        <button type="button" className="btn btn-yellow w-full" onClick={onNew}>
          + New conversation
        </button>
      </div>
      <ul className="contain min-h-0 flex-1 space-y-2 overflow-y-auto px-3 pb-3">
        {sessions.length === 0 && <li className="text-soft">No conversations yet.</li>}
        {sessions.map((s) => {
          const active = s.id === activeId;
          return (
            <li key={s.id}>
              {editing === s.id ? (
                <form
                  className="flex gap-2"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (draft.trim()) onRename(s.id, draft.trim());
                    setEditing(null);
                  }}
                >
                  <label className="sr-only" htmlFor={`rename-${s.id}`}>
                    New title
                  </label>
                  <input
                    id={`rename-${s.id}`}
                    autoFocus
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onKeyDown={(e) => e.key === "Escape" && setEditing(null)}
                    className="box-flat min-w-0 flex-1 px-2 py-1"
                  />
                  <button className="btn" type="submit">
                    OK
                  </button>
                </form>
              ) : confirm === s.id ? (
                <div className="box-flat space-y-2 p-2" role="alertdialog" aria-label="Confirm delete">
                  <p className="font-bold">Delete “{s.title.slice(0, 30)}”?</p>
                  <div className="flex gap-2">
                    <button className="btn btn-red flex-1" onClick={() => (setConfirm(null), onDelete(s.id))}>
                      Delete
                    </button>
                    <button className="btn flex-1" onClick={() => setConfirm(null)}>
                      Keep
                    </button>
                  </div>
                </div>
              ) : (
                <div className={`group flex items-stretch border-2 border-line ${active ? "bg-blue text-on-accent shadow-[3px_3px_0_var(--line)]" : "bg-paper hover:bg-sunk"}`}>
                  <button
                    type="button"
                    onClick={() => onSelect(s.id)}
                    aria-current={active ? "page" : undefined}
                    className="min-w-0 flex-1 truncate px-3 py-2 text-left"
                    title={s.title}
                  >
                    {busyIds.has(s.id) && <span className="mr-2 inline-block h-2.5 w-2.5 rounded-full bg-yellow align-middle pulse" aria-label="running" />}
                    {s.title}
                  </button>
                  <button
                    type="button"
                    className="px-2 opacity-60 hover:opacity-100 focus-visible:opacity-100"
                    aria-label={`Rename ${s.title}`}
                    onClick={() => (setDraft(s.title), setEditing(s.id))}
                  >
                    ✎
                  </button>
                  <button
                    type="button"
                    className="px-2 opacity-60 hover:opacity-100 focus-visible:opacity-100"
                    aria-label={`Delete ${s.title}`}
                    onClick={() => setConfirm(s.id)}
                  >
                    ✕
                  </button>
                </div>
              )}
            </li>
          );
        })}
      </ul>
      <div className="border-t-2 border-line p-3">
        <button type="button" className="btn btn-ghost w-full" onClick={onDisconnect}>
          Disconnect
        </button>
      </div>
    </nav>
  );
}
