"use client";

import { useEffect, useState } from "react";
import { api, type Conn } from "@/lib/api";
import { CAPPED, type Item } from "@/lib/events";
import { useLive } from "@/lib/useLive";
import { AssistantBubble, FailureCard, ReasoningCard, SubagentCard, ToolCard } from "./Cards";
import { Mark, type MarkKind } from "./Mark";

type Tool = Extract<Item, { kind: "tool" }>;
export type OpenSubagent = { runId: string; agent: string; task: string };

/**
 * One subagent's own run, beside the conversation: its tool calls, reasoning and replies as they
 * happen (the server replays the stored events first, so a finished subagent shows the same way).
 * A subagent it started shows as a card that opens in this panel.
 */
export function SubagentPanel({
  conn,
  open,
  onOpen,
  onDecide,
  onClose,
  onError,
}: {
  conn: Conn;
  open: OpenSubagent;
  onOpen: (next: OpenSubagent) => void;
  onDecide: (tool: Tool, approve: boolean) => void;
  onClose: () => void;
  onError: (e: Error) => void;
}) {
  const live = useLive(conn, () => {}, onError);
  const { follow, detach } = live;
  useEffect(() => {
    follow(open.runId);
    return detach;
  }, [open.runId, follow, detach]);

  const { phase, items } = live.state;
  // The event log cuts long replies short; once the run is over, show them whole from the
  // subagent's own transcript.
  const [whole, setWhole] = useState<string[]>([]);
  const finished = phase === "completed" || phase === "failed" || phase === "cancelled";
  const cut = items.some((i) => i.kind === "assistant" && CAPPED.test(i.text));
  useEffect(() => {
    setWhole([]);
  }, [open.runId]);
  useEffect(() => {
    if (!finished || !cut) return;
    let current = true;
    api
      .run(conn, open.runId)
      .then((run) => api.session(conn, run.session_id))
      .then((detail) => current && setWhole(detail.history.filter((h) => h.kind === "assistant").map((h) => h.text)))
      .catch(() => {}); // the shortened text stays; it says it was cut
    return () => {
      current = false;
    };
  }, [conn, open.runId, finished, cut]);
  const full = (text: string) => {
    if (!CAPPED.test(text)) return text;
    const start = text.replace(CAPPED, "");
    return whole.find((w) => w.startsWith(start)) ?? text;
  };
  const mark: MarkKind = phase === "running" ? "running" : phase === "completed" ? "ok" : phase === "failed" ? "failed" : "idle";
  const tools = items.filter((i) => i.kind === "tool" && i.depth === 0).length;
  return (
    <aside className="flex h-full min-h-0 flex-col border-l-2 border-line bg-surface" aria-label={`Subagent ${open.agent}`}>
      <header className="flex items-center gap-3 border-b-2 border-line px-4 py-3">
        <Mark kind={mark} />
        <h2 className="min-w-0 flex-1 truncate text-lg font-bold">
          subagent · {open.agent} <span className="code text-sm font-normal text-soft">{phase === "running" ? "running" : phase} · {tools} tools</span>
        </h2>
        <button type="button" className="btn" onClick={onClose} aria-label="Close the subagent panel">
          ✕
        </button>
      </header>
      {open.task && (
        <details className="border-b-2 border-line px-4 py-2">
          <summary className="cursor-pointer font-bold">Task</summary>
          <p className="mt-2 max-h-48 overflow-y-auto whitespace-pre-wrap text-sm">{open.task}</p>
        </details>
      )}
      <div className="contain min-h-0 flex-1 space-y-3 overflow-y-auto p-4">
        {items.length === 0 && <p className="text-soft">Loading the subagent's events…</p>}
        {items
          .filter((item) => item.depth === 0)
          .map((item) => {
            switch (item.kind) {
              case "tool":
                return <ToolCard key={item.key} tool={item} onDecide={onDecide} />;
              case "assistant":
                return <AssistantBubble key={item.key} text={full(item.text)} depth={0} streaming={item.streaming} />;
              case "reasoning":
                return <ReasoningCard key={item.key} item={item} />;
              case "subagent":
                return <SubagentCard key={item.key} item={item} onOpen={() => onOpen({ runId: item.runId, agent: item.agent, task: item.task })} />;
              case "failure":
                return <FailureCard key={item.key} text={item.text} />;
              case "notice":
                return <p key={item.key} className="code text-soft">↺ {item.text}</p>;
              case "user":
                return <p key={item.key} className="code text-soft">message from the parent: {item.text}</p>;
            }
          })}
      </div>
    </aside>
  );
}
