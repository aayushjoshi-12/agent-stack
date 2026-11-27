"use client";

import { useEffect, useRef, useState } from "react";
import { isTruncated, previewEdit, previewWrite } from "@/lib/diff";
import { toolLabel, type Item } from "@/lib/events";
import { DiffView } from "./DiffView";
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

/** What a write/edit is about to do, drawn as a diff, so the approval is informed. */
function ApprovalPreview({ tool }: { tool: Tool }) {
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  if (tool.tool === "edit_file") {
    const oldText = str(tool.args.old);
    const newText = str(tool.args.new);
    const cut = isTruncated(oldText) || isTruncated(newText);
    return (
      <div>
        <div className="text-xs font-bold uppercase tracking-wide text-soft">proposed change to {str(tool.args.path)}</div>
        <div className="box-flat mt-1 max-h-72 overflow-y-auto bg-surface">
          <DiffView rows={previewEdit(oldText, newText)} label="Proposed edit" />
        </div>
        {cut && <p className="code mt-1 text-soft">preview cut short (the log caps long text); the full change runs if you approve.</p>}
      </div>
    );
  }
  const content = str(tool.args.content);
  const { rows, hidden } = previewWrite(content);
  return (
    <div>
      <div className="text-xs font-bold uppercase tracking-wide text-soft">proposed new content for {str(tool.args.path)}</div>
      <div className="box-flat mt-1 max-h-72 overflow-y-auto bg-surface">
        <DiffView rows={rows} label="Proposed file content" />
      </div>
      {(hidden > 0 || isTruncated(content)) && <p className="code mt-1 text-soft">{hidden > 0 ? `… ${hidden} more lines. ` : ""}preview is partial; the whole file is written if you approve.</p>}
    </div>
  );
}

export function ToolCard({
  tool,
  onDecide,
  onOpenFile,
}: {
  tool: Tool;
  onDecide?: (tool: Tool, approve: boolean) => void;
  onOpenFile?: (path: string) => void;
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
  const filePath = typeof tool.args.path === "string" ? tool.args.path : null;
  const writes = (tool.tool === "write_file" || tool.tool === "edit_file") && filePath !== null;
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
            {waiting && writes && <ApprovalPreview tool={tool} />}
            {tool.status === "ok" && writes && filePath && onOpenFile && (
              <button type="button" className="btn btn-yellow" onClick={() => onOpenFile(filePath)}>
                View changes to {filePath.split("/").pop()}
              </button>
            )}
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
        {tool.auto && tool.auto.verdict !== "ask" && (
          <div className={`code border-t-2 border-line px-3 py-1 ${tool.auto.verdict === "deny" ? "text-red" : "text-soft"}`}>
            auto mode {tool.auto.verdict === "allow" ? "approved" : "blocked"} this: {tool.auto.reason}
          </div>
        )}
        {tool.approval?.note && waiting && (
          <div className="code border-t-2 border-line px-3 py-1 text-soft">Auto mode left this to you: {tool.approval.note}</div>
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
          {open ? "hide reasoning" : item.streaming ? "thinking…" : "show reasoning"}
        </button>
        {open && <p className="mt-2 whitespace-pre-wrap border-l-4 border-line/40 pl-3 text-sm">{item.text}</p>}
      </div>
    </Indent>
  );
}

export function AssistantBubble({ text, depth = 0, streaming = false }: { text: string; depth?: number; streaming?: boolean }) {
  return (
    <Indent depth={depth}>
      <section className="box p-4" aria-label="Assistant">
        <div className="mb-2 flex items-center gap-2 text-xs font-bold uppercase tracking-wide">
          <span className="inline-block h-3 w-3 bg-blue" aria-hidden />
          agent
        </div>
        {/* while it streams, plain text: Markdown is parsed once, on the final message */}
        {streaming ? <p className="whitespace-pre-wrap" aria-busy>{text}</p> : <Markdown text={text} />}
      </section>
    </Indent>
  );
}

export function UserBubble({
  text,
  queued = false,
  onEdit,
  onCancel,
  onRewind,
  rewinding = false,
}: {
  text: string;
  queued?: boolean;
  /** A queued message only: take it back into the composer / drop it. */
  onEdit?: () => void;
  onCancel?: () => void;
  /** A past message: open the rewind choices for it (`rewinding` while they are open). */
  onRewind?: () => void;
  rewinding?: boolean;
}) {
  return (
    <section
      className={`group ml-auto max-w-[85%] border-2 border-line bg-yellow p-3 text-on-yellow shadow-[4px_4px_0_var(--line)] ${queued ? "opacity-70" : ""}`}
      aria-label={queued ? "You (queued)" : "You"}
    >
      <div className="mb-1 flex items-baseline gap-3 text-xs font-bold uppercase tracking-wide">
        <span className="flex-1">{queued ? "you · queued" : "you"}</span>
        {onEdit && (
          <button type="button" className="underline" onClick={onEdit}>
            edit
          </button>
        )}
        {onCancel && (
          <button type="button" className="underline" onClick={onCancel}>
            cancel
          </button>
        )}
        {onRewind && (
          <button
            type="button"
            className={`underline ${rewinding ? "" : "opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 pointer-coarse:opacity-100"}`}
            onClick={onRewind}
            aria-expanded={rewinding}
            aria-label="Rewind to before this message"
            title="Rewind to before this message (Esc Esc opens it at the latest)"
          >
            ↺ rewind
          </button>
        )}
      </div>
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
