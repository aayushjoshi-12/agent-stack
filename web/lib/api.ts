import type { ApiEvent } from "./events.ts";

export type Conn = { base: string; token: string };
export type SessionSummary = { id: string; title: string };
export type HistoryRow = { kind: "user" | "assistant" | "tool"; text: string; args: Record<string, unknown> | null; result: string | null };
export type SessionDetail = { id: string; title: string | null; branch_of: string | null; history: HistoryRow[] };
export type RunInfo = { id: string; session_id: string; status: string; steps: number; tool_calls: number; error: string | null; parent_run_id: string | null };

export type UsageInfo = {
  calls: number;
  priced_calls: number;
  input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  cost_usd: number;
  budget_usd: number | null;
  by_model: Record<string, number>;
  models_summary: string;
  summary: string;
};

export type ChangedFile = { path: string; kind: "created" | "modified"; edits: number; added: number; removed: number };
export type ChangedFileDetail = ChangedFile & {
  diff: string;
  diff_truncated: boolean;
  before: string | null;
  after: string;
};

export class ApiError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function call<T>(conn: Conn, path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${conn.base}${path}`, {
      ...init,
      headers: { Authorization: `Bearer ${conn.token}`, "Content-Type": "application/json", ...init.headers },
    });
  } catch {
    throw new ApiError("Cannot reach the agent-stack server. Is `agent-stack web` running?", 0);
  }
  if (response.status === 401) throw new ApiError("The token was rejected.", 401);
  if (!response.ok) {
    const detail = await response.json().then((j: { detail?: unknown }) => String(j.detail ?? ""), () => "");
    throw new ApiError(detail || `Request failed (${response.status})`, response.status);
  }
  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

const json = (body: unknown): RequestInit => ({ method: "POST", body: JSON.stringify(body) });

export const api = {
  health: async (base: string): Promise<boolean> => {
    try {
      return (await fetch(`${base}/health`)).ok;
    } catch {
      return false;
    }
  },
  sessions: (c: Conn) => call<SessionSummary[]>(c, "/sessions?limit=100"),
  newSession: (c: Conn) => call<{ id: string }>(c, "/sessions", { method: "POST" }),
  session: (c: Conn, id: string) => call<SessionDetail>(c, `/sessions/${encodeURIComponent(id)}`),
  rename: (c: Conn, id: string, title: string) =>
    call<{ id: string }>(c, `/sessions/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ title }) }),
  remove: (c: Conn, id: string) => call<void>(c, `/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),
  send: (c: Conn, id: string, text: string) =>
    call<{ run_id: string }>(c, `/sessions/${encodeURIComponent(id)}/messages`, json({ text })),
  branch: (c: Conn, id: string, eventId: string) =>
    call<{ id: string }>(c, `/sessions/${encodeURIComponent(id)}/branch`, json({ event_id: eventId })),
  changes: (c: Conn, id: string) =>
    call<{ files: ChangedFile[]; note: string }>(c, `/sessions/${encodeURIComponent(id)}/changes`),
  changeFile: (c: Conn, id: string, path: string) =>
    call<ChangedFileDetail>(c, `/sessions/${encodeURIComponent(id)}/changes/file?path=${encodeURIComponent(path)}`),
  usage: (c: Conn, id: string) => call<UsageInfo>(c, `/sessions/${encodeURIComponent(id)}/usage`),
  run: (c: Conn, id: string) => call<RunInfo>(c, `/runs/${encodeURIComponent(id)}`),
  cancel: (c: Conn, id: string) => call<{ runs: Record<string, string> }>(c, `/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  decide: (c: Conn, runId: string, approvalId: string, approve: boolean) =>
    call<{ approved: boolean }>(
      c,
      `/runs/${encodeURIComponent(runId)}/approvals/${encodeURIComponent(approvalId)}`,
      json({ approve }),
    ),
};

/** Parse a Server-Sent Events byte stream into events. Exported for tests. */
export function parseSse(buffer: string): { events: ApiEvent[]; rest: string } {
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: ApiEvent[] = [];
  for (const block of blocks) {
    const data = block
      .split("\n")
      .filter((line) => line.startsWith("data: "))
      .map((line) => line.slice(6))
      .join("\n");
    if (!data) continue; // a keep-alive comment
    try {
      events.push(JSON.parse(data) as ApiEvent);
    } catch {
      // a malformed block is skipped rather than ending the stream
    }
  }
  return { events, rest };
}

/**
 * Stream one run's events from `after` (exclusive). Resolves when the server ends the stream
 * (the run finished) and rejects on a network error, so the caller can reconnect from the last
 * sequence it saw.
 */
export async function streamRun(
  conn: Conn,
  runId: string,
  after: number | null,
  onEvent: (event: ApiEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = { Authorization: `Bearer ${conn.token}` };
  if (after !== null) headers["Last-Event-ID"] = String(after);
  const response = await fetch(`${conn.base}/runs/${encodeURIComponent(runId)}/events`, { headers, signal });
  if (!response.ok || !response.body) throw new ApiError(`Stream failed (${response.status})`, response.status);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    const parsed = parseSse(buffer);
    buffer = parsed.rest;
    parsed.events.forEach(onEvent);
  }
}
