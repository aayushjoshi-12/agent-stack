"use client";

import { useEffect, useRef, useState } from "react";
import { toolLabel, type Item } from "@/lib/events";
import { Mark, type MarkKind } from "./Mark";
import { Markdown } from "./Markdown";

type Tool = Extract<Item, { kind: "tool" }>;

function Indent({ depth, children }: { depth: number; children: React.ReactNode }) {
  if (depth === 0) return <>{children}</>;
  return (
    <div className="border-l-4 border-line/40 pl-3" style={{ marginLeft: Math.min(depth, 4) * 12 }}>
      {children}
    </div>
  );
}

export function ToolCard({
  tool,
  onDecide,
}: {
  tool: Tool;
  onDecide?: (tool: Tool, approve: boolean) => void;
}) {
  const waiting = tool.approval?.state === "pending";
  const [open, setOpen] = useState(false);
  const bar = useRef<HTMLDivElement>(null);
  // A decision is needed: take focus off the composer so the y / n shortcuts work instead of
  // typing into it. The bar itself is focused (not a button), so a stray Enter cannot approve.
  useEffect(() => {
    if (waiting) bar.current?.focus();
  }, [waiting]);
  const mark: MarkKind = waiting ? "approval" : tool.status === "ok" ? "ok" : tool.status === "failed" ? "failed" : "running";
  const showBody = open || waiting;
  return (
    <Indent depth={tool.depth}>
      <article className={`box-flat ${waiting ? "border-yellow shadow-[4px_4px_0_var(--yellow)]" : ""}`} aria-label={`Tool call: ${tool.tool}`}>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={showBody}
          className="flex w-full items-center gap-3 px-3 py-2 text-left"
        >
          <Mark kind={mark} />
          <span className="code min-w-0 flex-1 truncate">{toolLabel(tool.tool, tool.args)}</span>
          <span className="code text-soft" aria-hidden>
            {showBody ? "−" : "+"}
          </span>
        </button>
        {showBody && (
          <div className="space-y-2 border-t-2 border-line px-3 py-3">
            <div>
              <div className="text-xs font-bold uppercase tracking-wide text-soft">arguments</div>
              <pre className="code mt-1 max-h-56 overflow-auto bg-sunk p-2 whitespace-pre-wrap break-all">
                {JSON.stringify(tool.args, null, 2)}
              </pre>
            </div>
            {(tool.output || tool.status !== "running") && (
              <div>
                <div className="text-xs font-bold uppercase tracking-wide text-soft">
                  output{tool.truncated ? " (truncated)" : ""}
                </div>
                <pre className="code mt-1 max-h-72 overflow-auto bg-sunk p-2 whitespace-pre-wrap break-all">
                  {tool.output || (tool.error ? "" : "(no output)")}
                </pre>
              </div>
            )}
            {tool.error && <p className="code text-red">error: {tool.error}</p>}
          </div>
        )}
        {waiting && onDecide && (
          <div
            ref={bar}
            tabIndex={-1}
            className="flex items-center gap-3 border-t-2 border-line bg-yellow px-3 py-3 text-on-yellow outline-none focus-visible:outline-3 focus-visible:outline-offset-[-6px] focus-visible:outline-blue"
            role="group"
            aria-label="Approval needed"
          >
            <span className="mr-auto font-bold">The agent wants to run this. Approve?</span>
            <button type="button" className="btn btn-ghost" onClick={() => onDecide(tool, false)}>
              Reject <kbd className="code opacity-60">n</kbd>
            </button>
            <button type="button" className="btn" onClick={() => onDecide(tool, true)}>
              Approve <kbd className="code opacity-60">y</kbd>
            </button>
          </div>
        )}
        {tool.approval && !waiting && (
          <div className="code border-t-2 border-line px-3 py-1 text-soft">
            approval: {tool.approval.state}
          </div>
        )}
      </article>
    </Indent>
  );
}

export function SubagentCard({ item }: { item: Extract<Item, { kind: "subagent" }> }) {
  const mark: MarkKind = item.status === "running" ? "running" : item.status === "completed" ? "ok" : "failed";
  return (
    <Indent depth={item.depth}>
      <div className="box-flat flex items-center gap-3 bg-sunk px-3 py-2">
        <Mark kind={mark} />
        <span className="font-bold">subagent · {item.agent}</span>
        <span className="code ml-auto text-soft">
          {item.status}
          {item.status !== "running" ? ` · ${item.steps} steps · ${item.toolCalls} tools` : ""}
        </span>
      </div>
    </Indent>
  );
}

export function ReasoningCard({ item }: { item: Extract<Item, { kind: "reasoning" }> }) {
  const [open, setOpen] = useState(false);
  return (
    <Indent depth={item.depth}>
      <div className="text-soft">
        <button type="button" onClick={() => setOpen((v) => !v)} aria-expanded={open} className="code underline decoration-dotted underline-offset-4">
          {open ? "hide reasoning" : "show reasoning"}
        </button>
        {open && <p className="mt-2 whitespace-pre-wrap border-l-4 border-line/40 pl-3 text-sm">{item.text}</p>}
      </div>
    </Indent>
  );
}

export function AssistantBubble({ text, depth = 0 }: { text: string; depth?: number }) {
  return (
    <Indent depth={depth}>
      <section className="box p-4" aria-label="Assistant">
        <div className="mb-2 flex items-center gap-2 text-xs font-bold uppercase tracking-wide">
          <span className="inline-block h-3 w-3 bg-blue" aria-hidden />
          agent
        </div>
        <Markdown text={text} />
      </section>
    </Indent>
  );
}

export function UserBubble({ text }: { text: string }) {
  return (
    <section className="ml-auto max-w-[85%] border-2 border-line bg-yellow p-3 text-on-yellow shadow-[4px_4px_0_var(--line)]" aria-label="You">
      <div className="mb-1 text-xs font-bold uppercase tracking-wide">you</div>
      <p className="whitespace-pre-wrap break-words">{text}</p>
    </section>
  );
}

export function FailureCard({ text }: { text: string }) {
  return (
    <div className="box-flat flex gap-3 border-red bg-surface p-3" role="alert">
      <Mark kind="failed" />
      <p className="code break-all text-red">{text}</p>
    </div>
  );
}
