"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, streamRun, type Conn } from "./api";
import { applyEvent, emptyLive, type ApiEvent, type LiveState, type RunPhase } from "./events";

const TERMINAL_STATUS = new Set(["completed", "failed", "cancelled"]);

function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}

/**
 * Follows one run (and the subagent runs it spawns) over SSE. A dropped connection is retried
 * from the last sequence seen per run, so the transcript never loses or repeats an event.
 */
export function useLive(conn: Conn | null, onFinished: (phase: RunPhase) => void, onFatal: (e: Error) => void) {
  const [state, setState] = useState<LiveState>(emptyLive);
  const stateRef = useRef<LiveState>(state);
  const aborts = useRef<Map<string, AbortController>>(new Map());
  const rootRun = useRef<string | null>(null);
  const callbacks = useRef({ onFinished, onFatal });
  callbacks.current = { onFinished, onFatal };

  const commit = useCallback((next: LiveState) => {
    stateRef.current = next;
    setState(next);
  }, []);

  const stopAll = useCallback(() => {
    aborts.current.forEach((c) => c.abort());
    aborts.current.clear();
  }, []);

  const watch = useCallback(
    (runId: string, isRoot: boolean) => {
      if (!conn || aborts.current.has(runId)) return;
      const controller = new AbortController();
      aborts.current.set(runId, controller);
      const { signal } = controller;

      const push = (event: ApiEvent) => {
        commit(applyEvent(stateRef.current, event));
        if (event.type === "subagent.started" && typeof event.data.child_run_id === "string") {
          watch(event.data.child_run_id, false);
        }
      };

      void (async () => {
        let backoff = 500;
        while (!signal.aborted) {
          let terminal = false;
          try {
            await streamRun(
              conn,
              runId,
              stateRef.current.cursor[runId] ?? null,
              (event) => {
                if (event.type === "run.completed" || event.type === "run.failed") terminal = true;
                push(event);
              },
              signal,
            );
            if (terminal) break;
            // The stream ended without a terminal event: a hard cancel publishes none, so ask.
            const info = await api.run(conn, runId);
            if (TERMINAL_STATUS.has(info.status)) {
              if (isRoot && stateRef.current.phase === "running") {
                const phase: RunPhase = info.status === "completed" ? "completed" : info.status === "failed" ? "failed" : "cancelled";
                commit({ ...stateRef.current, phase });
              }
              break;
            }
          } catch (error) {
            if (signal.aborted) return;
            if (error instanceof ApiError && (error.status === 401 || error.status === 404)) {
              callbacks.current.onFatal(error);
              break;
            }
          }
          await pause(backoff, signal);
          backoff = Math.min(backoff * 2, 8000);
        }
        aborts.current.delete(runId);
        if (isRoot && !signal.aborted) callbacks.current.onFinished(stateRef.current.phase);
      })();
    },
    [conn, commit],
  );

  /** Start following a run from its first event (the server replays history, then goes live). */
  const follow = useCallback(
    (runId: string) => {
      stopAll();
      rootRun.current = runId;
      commit({ ...emptyLive(), root: runId, phase: "running" });
      watch(runId, true);
    },
    [stopAll, commit, watch],
  );

  /** Stop watching (the run itself keeps going on the server) and clear the live view. */
  const detach = useCallback(() => {
    stopAll();
    rootRun.current = null;
    commit(emptyLive());
  }, [stopAll, commit]);

  useEffect(() => stopAll, [stopAll]);

  return { state, follow, detach, rootRun };
}
