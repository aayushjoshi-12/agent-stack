"use client";

import { useState } from "react";
import { api, ApiError, type Conn } from "@/lib/api";

export const DEFAULT_BASE = "http://127.0.0.1:8765";

export function Connect({ onConnected, initialError }: { onConnected: (c: Conn) => void; initialError?: string }) {
  const [base, setBase] = useState(DEFAULT_BASE);
  const [token, setToken] = useState("");
  const [error, setError] = useState(initialError ?? "");
  const [busy, setBusy] = useState(false);

  async function submit() {
    const conn = { base: base.trim().replace(/\/+$/, ""), token: token.trim() };
    setBusy(true);
    setError("");
    try {
      if (!(await api.health(conn.base))) throw new ApiError("Cannot reach the server. Start it with `agent-stack web`.", 0);
      await api.sessions(conn); // proves the token before we keep it
      onConnected(conn);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not connect.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="grid min-h-full place-items-center p-4">
      <form
        className="box w-full max-w-md space-y-5 p-6"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <div className="flex items-center gap-2">
          <span className="inline-block h-5 w-5 bg-red" aria-hidden />
          <span className="inline-block h-5 w-5 rounded-full bg-yellow" aria-hidden />
          <span className="inline-block h-5 w-5 bg-blue [clip-path:polygon(50%_0,100%_100%,0_100%)]" aria-hidden />
        </div>
        <div>
          <h1 className="text-3xl font-bold tracking-tight">agent-stack</h1>
          <p className="mt-1 text-soft">Connect to your local agent. The token stays in this tab only.</p>
        </div>
        <div className="space-y-1">
          <label htmlFor="base" className="font-bold">
            Server
          </label>
          <input id="base" value={base} onChange={(e) => setBase(e.target.value)} className="code box-flat w-full px-3 py-2" autoComplete="off" spellCheck={false} />
        </div>
        <div className="space-y-1">
          <label htmlFor="token" className="font-bold">
            Token <span className="font-normal text-soft">(AGENT_STACK_WEB_TOKEN)</span>
          </label>
          <input
            id="token"
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            className="code box-flat w-full px-3 py-2"
            autoComplete="off"
            spellCheck={false}
            autoFocus
          />
        </div>
        {error && (
          <p role="alert" className="border-2 border-line bg-red p-3 font-bold text-on-accent">
            {error}
          </p>
        )}
        <button type="submit" className="btn btn-blue w-full" disabled={busy || token.trim().length < 8}>
          {busy ? "Connecting…" : "Connect"}
        </button>
      </form>
    </main>
  );
}
