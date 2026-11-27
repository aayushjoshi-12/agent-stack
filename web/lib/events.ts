// The live view of a run, built from the server's event stream. Pure and framework-free so it
// can be tested with `node --test`. Events may arrive twice (a reconnect resumes by sequence,
// but a retry can overlap), so every event is applied at most once per run.

export type ApiEvent = {
  id: string;
  type: string;
  run_id: string;
  session_id: string;
  sequence: number;
  timestamp: string;
  parent_run_id: string | null;
  data: Record<string, unknown>;
};

export type ToolStatus = "running" | "ok" | "failed";
export type ApprovalState = "pending" | "approved" | "rejected" | "cancelled";

export type Item =
  | { kind: "reasoning"; key: string; runId: string; depth: number; text: string; streaming?: boolean }
  | {
      kind: "assistant";
      key: string;
      runId: string;
      depth: number;
      text: string;
      eventId: string;
      /** Still being written (assistant.delta); the final assistant.message replaces it. */
      streaming?: boolean;
    }
  | {
      kind: "tool";
      key: string;
      runId: string;
      depth: number;
      callId: string;
      tool: string;
      args: Record<string, unknown>;
      output: string;
      truncated: boolean;
      status: ToolStatus;
      error: string | null;
      approval: { id: string; state: ApprovalState; note?: string } | null;
      /** What auto mode decided about this call, when it did. */
      auto: { verdict: string; reason: string } | null;
    }
  | {
      kind: "subagent";
      key: string;
      runId: string;
      depth: number;
      agent: string;
      task: string;
      status: string;
      steps: number;
      toolCalls: number;
    }
  | { kind: "user"; key: string; runId: string; depth: number; text: string }
  | { kind: "failure"; key: string; runId: string; depth: number; text: string }
  | { kind: "notice"; key: string; runId: string; depth: number; text: string };

export type RunPhase = "idle" | "running" | "completed" | "failed" | "cancelled";

export type LiveState = {
  items: Item[];
  /** Highest sequence applied per run: the resume cursor and the duplicate filter. */
  cursor: Record<string, number>;
  /** Runs seen so far, with their parent (null for the root run). */
  runs: Record<string, string | null>;
  /** The run this view follows. A subagent panel follows a child run, which has a parent. */
  root: string | null;
  phase: RunPhase;
  /** Id of the run.completed event, so the UI can offer "fork here". */
  completedEventId: string | null;
  /** Counts model.usage events: the UI refetches the conversation's cost whenever it moves. */
  usageTick: number;
  /** Counts finished write_file/edit_file calls: the UI refetches the changed-files list. */
  changeTick: number;
  /** assistant.message events seen per run: the index of the step now streaming. */
  finals: Record<string, number>;
};

export const OUTPUT_CAP = 20_000;

export const emptyLive = (): LiveState => ({
  items: [],
  cursor: {},
  runs: {},
  root: null,
  phase: "idle",
  completedEventId: null,
  usageTick: 0,
  changeTick: 0,
  finals: {},
});

const str = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);
const num = (v: unknown): number => (typeof v === "number" ? v : 0);
const record = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};

/** The followed run: the one with no parent, or the subagent run a panel follows. */
const isRoot = (state: LiveState, event: ApiEvent) => event.parent_run_id === null || event.run_id === state.root;

function depthOf(state: LiveState, runId: string): number {
  let depth = 0;
  let current: string | null | undefined = runId === state.root ? null : state.runs[runId];
  while (current && current !== state.root && depth < 8) {
    depth += 1;
    current = state.runs[current];
  }
  if (current === state.root && current !== null && depth < 8) depth += 1;
  return depth;
}

function updateTool(
  items: Item[],
  runId: string,
  callId: string,
  change: (tool: Extract<Item, { kind: "tool" }>) => Extract<Item, { kind: "tool" }>,
): Item[] | null {
  const at = items.findIndex((i) => i.kind === "tool" && i.runId === runId && i.callId === callId);
  if (at < 0) return null;
  const found = items[at];
  if (!found || found.kind !== "tool") return null;
  const next = items.slice();
  next[at] = change(found);
  return next;
}

/** The event log's cap marker on a long field (observability/audit.py `_cap`). */
const CAPPED = /\.\.\.\[truncated \d+ bytes\]$/;

const deltaKey = (runId: string, step: number, kind: "assistant" | "reasoning") => `delta:${runId}:${step}:${kind}`;

/** Put `item` where the step's streamed item is (it replaces it), or at the end. */
function settle(items: Item[], key: string, item: Item | null): Item[] {
  const at = items.findIndex((i) => i.key === key);
  if (at < 0) return item ? [...items, item] : items;
  const next = items.slice();
  if (item) next[at] = item;
  else next.splice(at, 1);
  return next;
}

/**
 * Grow the step's streaming assistant/reasoning item. Deltas are live-only (no sequence, never
 * replayed), so they skip the duplicate filter; a subagent's never show, and one that arrives
 * after its step's final message is dropped.
 */
function applyDelta(state: LiveState, event: ApiEvent): LiveState {
  const step = num(event.data.step);
  const text = str(event.data.text);
  if (!isRoot(state, event) || !text || step < (state.finals[event.run_id] ?? 0)) return state;
  const kind = event.data.kind === "reasoning" ? "reasoning" : "assistant";
  const key = deltaKey(event.run_id, step, kind);
  const at = state.items.findIndex((i) => i.key === key);
  const found = state.items[at];
  if (found && (found.kind === "assistant" || found.kind === "reasoning")) {
    const items = state.items.slice();
    items[at] = { ...found, text: found.text + text };
    return { ...state, items };
  }
  const item: Item =
    kind === "reasoning"
      ? { kind, key, runId: event.run_id, depth: 0, text, streaming: true }
      : { kind, key, runId: event.run_id, depth: 0, text, eventId: "", streaming: true };
  return { ...state, items: [...state.items, item] };
}

/** Apply one event; returns the same state object when the event is a duplicate. */
export function applyEvent(state: LiveState, event: ApiEvent): LiveState {
  if (event.type === "assistant.delta") return applyDelta(state, event);
  const last = state.cursor[event.run_id];
  if (last !== undefined && event.sequence <= last) return state;

  const runs = event.run_id in state.runs ? state.runs : { ...state.runs, [event.run_id]: event.parent_run_id };
  const base: LiveState = { ...state, runs, cursor: { ...state.cursor, [event.run_id]: event.sequence } };
  const depth = depthOf(base, event.run_id);
  const d = event.data;
  const key = event.id;

  switch (event.type) {
    case "run.started":
      return isRoot(base, event) ? { ...base, phase: "running" } : base;
    case "model.usage":
      return { ...base, usageTick: base.usageTick + 1 };
    case "model.reasoning": {
      const step = base.finals[event.run_id] ?? 0;
      const item: Item = { kind: "reasoning", key, runId: event.run_id, depth, text: str(d.text) };
      return { ...base, items: settle(base.items, deltaKey(event.run_id, step, "reasoning"), item) };
    }
    case "assistant.message": {
      const step = base.finals[event.run_id] ?? 0;
      const finals = { ...base.finals, [event.run_id]: step + 1 };
      const streamedKey = deltaKey(event.run_id, step, "assistant");
      const streamed = base.items.find((i) => i.key === streamedKey);
      let text = str(d.content).trim();
      // the logged copy is capped (P29); the streamed one is whole until history reloads
      if (CAPPED.test(text) && streamed?.kind === "assistant") text = streamed.text.trim();
      const item: Item | null = text
        ? { kind: "assistant", key, runId: event.run_id, depth, text, eventId: event.id }
        : null;
      // reasoning that streamed but never got its own final event stops streaming here
      const items = base.items.map((i) =>
        i.key === deltaKey(event.run_id, step, "reasoning") ? { ...i, streaming: false } : i,
      );
      return { ...base, finals, items: settle(items, streamedKey, item) };
    }
    case "tool.started":
      return {
        ...base,
        items: [
          ...base.items,
          {
            kind: "tool",
            key,
            runId: event.run_id,
            depth,
            callId: str(d.tool_call_id),
            tool: str(d.tool, "tool"),
            args: record(d.args),
            output: "",
            truncated: false,
            status: "running",
            error: null,
            approval: null,
            auto: null,
          },
        ],
      };
    case "tool.output": {
      const chunk = str(d.chunk);
      const items = updateTool(base.items, event.run_id, str(d.tool_call_id), (t) => {
        if (t.truncated) return t;
        const merged = t.output + chunk;
        return merged.length > OUTPUT_CAP
          ? { ...t, output: merged.slice(0, OUTPUT_CAP), truncated: true }
          : { ...t, output: merged };
      });
      return items ? { ...base, items } : base;
    }
    case "tool.completed": {
      const ok = d.ok !== false;
      const items = updateTool(base.items, event.run_id, str(d.tool_call_id), (t) => ({
        ...t,
        status: ok ? "ok" : "failed",
        error: ok ? null : str(d.error) || "failed",
      }));
      const touchesFiles = ok && (d.tool === "write_file" || d.tool === "edit_file");
      const counted = touchesFiles ? { ...base, changeTick: base.changeTick + 1 } : base;
      return items ? { ...counted, items } : counted;
    }
    case "approval.required": {
      const callId = str(d.tool_call_id);
      const approval = {
        id: str(d.approval_id),
        state: "pending" as ApprovalState,
        ...(str(d.note) ? { note: str(d.note) } : {}),
      };
      const items = updateTool(base.items, event.run_id, callId, (t) => ({ ...t, approval }));
      if (items) return { ...base, items };
      return {
        ...base,
        items: [
          ...base.items,
          {
            kind: "tool",
            key,
            runId: event.run_id,
            depth,
            callId,
            tool: str(d.tool, "tool"),
            args: record(d.args),
            output: "",
            truncated: false,
            status: "running",
            error: null,
            approval,
            auto: null,
          },
        ],
      };
    }
    case "approval.auto": {
      const auto = { verdict: str(d.verdict), reason: str(d.reason) };
      const items = updateTool(base.items, event.run_id, str(d.tool_call_id), (t) => ({ ...t, auto }));
      return items ? { ...base, items } : base;
    }
    case "approval.resolved": {
      const raw = str(d.status);
      const state: ApprovalState =
        raw === "approved" ? "approved" : raw === "rejected" ? "rejected" : "cancelled";
      const items = updateTool(base.items, event.run_id, str(d.tool_call_id), (t) =>
        t.approval ? { ...t, approval: { ...t.approval, state } } : t,
      );
      return items ? { ...base, items } : base;
    }
    case "subagent.started":
      return {
        ...base,
        items: [
          ...base.items,
          {
            kind: "subagent",
            key,
            runId: str(d.child_run_id),
            depth,
            agent: str(d.agent_name, "subagent"),
            task: str(d.task),
            status: "running",
            steps: 0,
            toolCalls: 0,
          },
        ],
      };
    case "subagent.completed": {
      const child = str(d.child_run_id);
      const items = base.items.map((i) =>
        i.kind === "subagent" && i.runId === child
          ? { ...i, status: str(d.status, "completed"), steps: num(d.steps), toolCalls: num(d.tool_calls) }
          : i,
      );
      return { ...base, items };
    }
    case "run.completed":
      return isRoot(base, event)
        ? { ...base, phase: "completed", completedEventId: event.id }
        : base;
    case "run.failed": {
      const error = str(d.error, "run failed");
      const cancelled = error.includes("RunCancelled");
      if (!isRoot(base, event)) return base;
      return {
        ...base,
        phase: cancelled ? "cancelled" : "failed",
        items: cancelled
          ? base.items
          : [...base.items, { kind: "failure", key, runId: event.run_id, depth, text: error.slice(0, 600) }],
      };
    }
    case "input.received":
      // A message queued while the run worked, shown where the model read it.
      return isRoot(base, event)
        ? { ...base, items: [...base.items, { kind: "user", key, runId: event.run_id, depth, text: str(d.text) }] }
        : base;
    case "context.compacted":
      return isRoot(base, event)
        ? {
            ...base,
            items: [...base.items, { kind: "notice", key, runId: event.run_id, depth, text: `context compacted: ${num(d.messages)} messages summarised` }],
          }
        : base;
    default:
      return base;
  }
}

/** Runs that are still going: the root plus any subagent without a final status. */
export function activeChildren(state: LiveState): string[] {
  return state.items.flatMap((i) => (i.kind === "subagent" && i.status === "running" ? [i.runId] : []));
}

/** One-line label for a tool call, like the TUI and Telegram show. */
export function toolLabel(tool: string, args: Record<string, unknown>, limit = 72): string {
  const command = args.command;
  const short = Object.values(args).find((v) => typeof v === "string" && v.length < 80);
  const detail = typeof command === "string" ? command : typeof short === "string" ? short : "";
  const text = `${tool} ${detail}`.trim().replace(/\s+/g, " ");
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}
