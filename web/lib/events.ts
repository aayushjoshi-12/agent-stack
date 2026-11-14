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
  | { kind: "reasoning"; key: string; runId: string; depth: number; text: string }
  | { kind: "assistant"; key: string; runId: string; depth: number; text: string; eventId: string }
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
      approval: { id: string; state: ApprovalState } | null;
    }
  | {
      kind: "subagent";
      key: string;
      runId: string;
      depth: number;
      agent: string;
      status: string;
      steps: number;
      toolCalls: number;
    }
  | { kind: "failure"; key: string; runId: string; depth: number; text: string };

export type RunPhase = "idle" | "running" | "completed" | "failed" | "cancelled";

export type LiveState = {
  items: Item[];
  /** Highest sequence applied per run: the resume cursor and the duplicate filter. */
  cursor: Record<string, number>;
  /** Runs seen so far, with their parent (null for the root run). */
  runs: Record<string, string | null>;
  phase: RunPhase;
  /** Id of the run.completed event, so the UI can offer "fork here". */
  completedEventId: string | null;
  /** Counts model.usage events: the UI refetches the conversation's cost whenever it moves. */
  usageTick: number;
};

export const OUTPUT_CAP = 20_000;

export const emptyLive = (): LiveState => ({
  items: [],
  cursor: {},
  runs: {},
  phase: "idle",
  completedEventId: null,
  usageTick: 0,
});

const str = (v: unknown, fallback = ""): string => (typeof v === "string" ? v : fallback);
const num = (v: unknown): number => (typeof v === "number" ? v : 0);
const record = (v: unknown): Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};

function depthOf(state: LiveState, runId: string): number {
  let depth = 0;
  let current: string | null | undefined = state.runs[runId];
  while (current && depth < 8) {
    depth += 1;
    current = state.runs[current];
  }
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

/** Apply one event; returns the same state object when the event is a duplicate. */
export function applyEvent(state: LiveState, event: ApiEvent): LiveState {
  const last = state.cursor[event.run_id];
  if (last !== undefined && event.sequence <= last) return state;

  const runs = event.run_id in state.runs ? state.runs : { ...state.runs, [event.run_id]: event.parent_run_id };
  const base: LiveState = { ...state, runs, cursor: { ...state.cursor, [event.run_id]: event.sequence } };
  const depth = depthOf(base, event.run_id);
  const d = event.data;
  const key = event.id;

  switch (event.type) {
    case "run.started":
      return event.parent_run_id === null ? { ...base, phase: "running" } : base;
    case "model.usage":
      return { ...base, usageTick: base.usageTick + 1 };
    case "model.reasoning":
      return {
        ...base,
        items: [...base.items, { kind: "reasoning", key, runId: event.run_id, depth, text: str(d.text) }],
      };
    case "assistant.message": {
      const text = str(d.content).trim();
      if (!text) return base;
      return {
        ...base,
        items: [...base.items, { kind: "assistant", key, runId: event.run_id, depth, text, eventId: event.id }],
      };
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
      return items ? { ...base, items } : base;
    }
    case "approval.required": {
      const callId = str(d.tool_call_id);
      const approval = { id: str(d.approval_id), state: "pending" as ApprovalState };
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
          },
        ],
      };
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
      return event.parent_run_id === null
        ? { ...base, phase: "completed", completedEventId: event.id }
        : base;
    case "run.failed": {
      const error = str(d.error, "run failed");
      const cancelled = error.includes("RunCancelled");
      if (event.parent_run_id !== null) return base;
      return {
        ...base,
        phase: cancelled ? "cancelled" : "failed",
        items: cancelled
          ? base.items
          : [...base.items, { kind: "failure", key, runId: event.run_id, depth, text: error.slice(0, 600) }],
      };
    }
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
