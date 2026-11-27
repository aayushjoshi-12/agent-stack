"use client";

import { useEffect, useRef } from "react";
import { formatWhen } from "@/lib/format";
import type { TurnInfo } from "@/lib/api";
import { MODES } from "./HistoryPanel";

export type RewindMode = (typeof MODES)[number];

/**
 * The rewind choices for one message in the chat, opened from its bubble or with Esc Esc.
 * Up / Down move to the message before or after (the terminal picker's cursor); Esc closes.
 */
export function RewindMenu({
  turn,
  busy,
  onPick,
  onMove,
  onClose,
}: {
  turn: TurnInfo;
  busy: boolean;
  onPick: (mode: RewindMode) => void;
  onMove: (step: -1 | 1) => void;
  onClose: () => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    box.current?.scrollIntoView({ block: "nearest" });
    box.current?.querySelector("button")?.focus();
  }, [turn]);

  return (
    <div
      ref={box}
      role="group"
      aria-label="Rewind to before this message"
      className="box ml-auto mt-2 max-w-[85%] space-y-2 p-3"
      onKeyDown={(e) => {
        if (e.key === "Escape") onClose();
        else if (e.key === "ArrowUp") onMove(-1);
        else if (e.key === "ArrowDown") onMove(1);
        else return;
        e.preventDefault();
        e.stopPropagation(); // the page's own Esc Esc must not count this press
      }}
    >
      <p className="font-bold">
        Rewind to before this message
        <span className="code ml-2 font-normal text-soft">
          {formatWhen(turn.timestamp)}
          {turn.files_changed > 0 && ` · ${turn.files_changed} file${turn.files_changed === 1 ? "" : "s"} changed`}
        </span>
      </p>
      {MODES.map((m) => (
        <button key={m.key} type="button" className="btn w-full text-left" disabled={busy} onClick={() => onPick(m)}>
          {m.label}
        </button>
      ))}
      <div className="flex items-center gap-3">
        <button type="button" className="btn btn-ghost" onClick={onClose}>
          Cancel
        </button>
        <span className="code text-soft">↑ ↓ another message · esc closes</span>
      </div>
    </div>
  );
}
