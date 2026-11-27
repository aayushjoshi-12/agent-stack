"use client";

import { useState } from "react";
import type { MemorySuggestion } from "@/lib/api";

type Keep = (item: MemorySuggestion, edit?: { key: string; value: string }) => Promise<void>;
type Reject = (item: MemorySuggestion) => Promise<void>;

/**
 * Facts the assistant suggested remembering (P53), above the composer. Nothing is stored until
 * the user keeps one; a rejected one is not suggested again.
 */
export function MemorySuggestions({ items, onKeep, onReject }: { items: MemorySuggestion[]; onKeep: Keep; onReject: Reject }) {
  if (items.length === 0) return null;
  return (
    <section aria-label="Memory suggestions" className="box-flat mb-3 space-y-2 bg-sunk p-3">
      <p className="font-bold">
        Remember for next time?
        <span className="code ml-2 font-normal text-soft">nothing is kept until you say so</span>
      </p>
      {items.map((item) => (
        <Suggestion key={item.id} item={item} onKeep={onKeep} onReject={onReject} />
      ))}
    </section>
  );
}

function Suggestion({ item, onKeep, onReject }: { item: MemorySuggestion; onKeep: Keep; onReject: Reject }) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [key, setKey] = useState(item.key);
  const [value, setValue] = useState(item.value);
  const act = (work: Promise<void>) => {
    setBusy(true);
    void work.finally(() => setBusy(false));
  };

  if (editing) {
    return (
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (key.trim() && value.trim()) act(onKeep(item, { key: key.trim(), value: value.trim() }));
        }}
      >
        <input value={key} onChange={(e) => setKey(e.target.value)} aria-label="Memory name" className="code w-40 border-2 border-line bg-surface p-2" />
        <input value={value} onChange={(e) => setValue(e.target.value)} aria-label="Memory" className="code min-w-0 flex-1 border-2 border-line bg-surface p-2" />
        <button type="submit" className="btn btn-yellow" disabled={busy || !key.trim() || !value.trim()}>
          Save
        </button>
        <button type="button" className="btn btn-ghost" onClick={() => setEditing(false)}>
          Cancel
        </button>
      </form>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-2">
      <p className="min-w-0 basis-full break-words sm:basis-0 sm:flex-1">
        <span className="code font-bold">{item.key}</span> {item.value}
      </p>
      <button type="button" className="btn btn-yellow" disabled={busy} onClick={() => act(onKeep(item))}>
        Keep
      </button>
      <button type="button" className="btn" disabled={busy} onClick={() => setEditing(true)}>
        Edit
      </button>
      <button type="button" className="btn btn-ghost" disabled={busy} onClick={() => act(onReject(item))}>
        Reject
      </button>
    </div>
  );
}
