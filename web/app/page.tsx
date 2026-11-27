"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AssistantBubble, FailureCard, ReasoningCard, SubagentCard, ToolCard, UserBubble } from "@/components/Cards";
import { Composer } from "@/components/Composer";
import { Connect } from "@/components/Connect";
import { Mark } from "@/components/Mark";
import { Sidebar } from "@/components/Sidebar";
import { api, ApiError, type ChangedFile, type Conn, type HistoryRow, type RewindResult, type SessionSummary, type SlashCommand, type UsageInfo } from "@/lib/api";
import { ChangesPanel } from "@/components/ChangesPanel";
import { HistoryPanel, type HistoryTab } from "@/components/HistoryPanel";
import { formatCost, formatTokens } from "@/lib/format";
import type { Item, RunPhase } from "@/lib/events";
import { useLive } from "@/lib/useLive";

type Tool = Extract<Item, { kind: "tool" }>;

const STORE = "agent-stack:conn";

function loadConn(): Conn | null {
  try {
    const raw = sessionStorage.getItem(STORE);
    return raw ? (JSON.parse(raw) as Conn) : null;
  } catch {
    return null;
  }
}
function saveConn(conn: Conn | null) {
  try {
    if (conn) sessionStorage.setItem(STORE, JSON.stringify(conn));
    else sessionStorage.removeItem(STORE);
  } catch {
    // storage can be unavailable (private windows); the app works without it
  }
}

/** A stored tool call as a finished card. */
function historyTool(row: HistoryRow, index: number): Tool {
  return {
    kind: "tool",
    key: `h${index}`,
    runId: "",
    depth: 0,
    callId: `h${index}`,
    tool: row.text,
    args: row.args ?? {},
    output: row.result ?? "",
    truncated: false,
    // Stored rows carry no success flag; tools report failure as "Error: ..." text.
    status: row.result?.startsWith("Error") ? "failed" : "ok",
    error: null,
    approval: null,
    auto: null,
  };
}

export default function Page() {
  const [conn, setConn] = useState<Conn | null>(null);
  const [ready, setReady] = useState(false);
  const [connectError, setConnectError] = useState("");
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [pendingUser, setPendingUser] = useState<string | null>(null);
  /** Messages sent while the run worked that the model has not read yet. */
  const [queued, setQueued] = useState<string[]>([]);
  const [error, setError] = useState("");
  const [stopping, setStopping] = useState(false);
  const [usage, setUsage] = useState<UsageInfo | null>(null);
  const [changes, setChanges] = useState<{ files: ChangedFile[]; note: string }>({ files: [], note: "" });
  const [panelOpen, setPanelOpen] = useState(false);
  const [focusPath, setFocusPath] = useState<string | null>(null);
  const [drawer, setDrawer] = useState(false);
  const [forkable, setForkable] = useState<{ sessionId: string; eventId: string } | null>(null);
  const [historyTab, setHistoryTab] = useState<HistoryTab | null>(null);
  const [draft, setDraft] = useState<{ text: string; n: number } | null>(null);
  const [filesDiffer, setFilesDiffer] = useState(false);
  const [skills, setSkills] = useState<SlashCommand[]>([]);
  const [mode, setMode] = useState<string>("ask");
  const [notice, setNotice] = useState("");
  const draftCount = useRef(0);
  const lastEscape = useRef(0);
  const runBySession = useRef<Map<string, string>>(new Map());
  const activeRef = useRef<string | null>(null);
  activeRef.current = activeId;
  const scroller = useRef<HTMLDivElement>(null);

  const wanted = useRef<string | null>(null);
  useEffect(() => {
    wanted.current = new URLSearchParams(window.location.search).get("s"); // read before any sync
    setConn(loadConn());
    setReady(true);
  }, []);

  // The open conversation lives in the URL (?s=<id>), so a reload or a shared tab returns to it.
  useEffect(() => {
    if (!conn) return; // before connecting, leave the address alone
    const url = new URL(window.location.href);
    if (activeId) url.searchParams.set("s", activeId);
    else url.searchParams.delete("s");
    window.history.replaceState(null, "", url);
  }, [conn, activeId]);

  const fail = useCallback((e: unknown) => {
    if (e instanceof ApiError && e.status === 401) {
      saveConn(null);
      setConn(null);
      setConnectError("The token was rejected. Connect again.");
      return;
    }
    setError(e instanceof Error ? e.message : "Something went wrong.");
  }, []);

  const refreshSessions = useCallback(async () => {
    if (conn) setSessions(await api.sessions(conn));
  }, [conn]);

  const loadHistory = useCallback(
    async (id: string) => {
      if (!conn) return;
      const detail = await api.session(conn, id);
      if (activeRef.current === id) setHistory(detail.history);
    },
    [conn],
  );

  const live = useLive(
    conn,
    (phase: RunPhase) => {
      const id = activeRef.current;
      setStopping(false);
      setPendingUser(null);
      if (unreadRef.current.length) {
        // A stopped run never read them: hand them back to edit or resend.
        draftCount.current += 1;
        setDraft({ text: unreadRef.current.join("\n\n"), n: draftCount.current });
      }
      setQueued([]);
      if (id) runBySession.current.delete(id);
      if (id && phase === "completed" && live.state.completedEventId) setForkable({ sessionId: id, eventId: live.state.completedEventId });
      void (async () => {
        try {
          if (id) await loadHistory(id);
          await refreshSessions();
        } catch (e) {
          fail(e);
        } finally {
          live.detach();
        }
      })();
    },
    fail,
  );
  const running = live.state.phase === "running";

  // The cost follows the conversation: refetched when it is opened, whenever a model call lands,
  // and when a run ends. The server sums the durable log, so subagent spend is included.
  const usageTick = live.state.usageTick;
  useEffect(() => {
    if (!conn || !activeId) {
      setUsage(null);
      return;
    }
    let current = true;
    api
      .usage(conn, activeId)
      .then((u) => current && setUsage(u))
      .catch(() => current && setUsage(null)); // cost is informational: never block the chat
    return () => {
      current = false;
    };
  }, [conn, activeId, usageTick, running]);

  // The files the agent changed: refetched when a conversation opens, whenever a write/edit
  // tool finishes, and when a run ends.
  const changeTick = live.state.changeTick;
  useEffect(() => {
    if (!conn || !activeId) {
      setChanges({ files: [], note: "" });
      return;
    }
    let current = true;
    api
      .changes(conn, activeId)
      .then((r) => current && setChanges(r))
      .catch(() => current && setChanges({ files: [], note: "" })); // informational: never block the chat
    return () => {
      current = false;
    };
  }, [conn, activeId, changeTick, running]);

  // Esc Esc: clear what is typed, or open the rewind picker (the terminal UI's habit).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || historyTab || running || !activeRef.current) return;
      const now = Date.now();
      const double = now - lastEscape.current < 800;
      lastEscape.current = double ? 0 : now;
      if (!double) return;
      const box = document.getElementById("composer") as HTMLTextAreaElement | null;
      if (box && box.value.trim()) {
        draftCount.current += 1;
        setDraft({ text: "", n: draftCount.current });
      } else {
        setHistoryTab("rewind");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [historyTab, running]);

  // The folder may hold another conversation's files (after a rewind or switching chats).
  useEffect(() => {
    if (!conn || !activeId || running) {
      setFilesDiffer(false);
      return;
    }
    let current = true;
    api
      .filesStatus(conn, activeId)
      .then((r) => current && setFilesDiffer(r.differs))
      .catch(() => current && setFilesDiffer(false)); // informational
    return () => {
      current = false;
    };
  }, [conn, activeId, running, changeTick]);

  // The skills a user can type as /name, and the approval mode (ask you, or auto).
  useEffect(() => {
    if (!conn) return;
    api.commands(conn).then(setSkills).catch(() => setSkills([]));
    api.approvalMode(conn).then((r) => setMode(r.mode)).catch(() => undefined);
  }, [conn]);

  const toggleMode = useCallback(async () => {
    if (!conn) return;
    try {
      const next = await api.setApprovalMode(conn, mode === "auto" ? "ask" : "auto");
      setMode(next.mode);
      setNotice(
        next.mode === "auto"
          ? "Auto mode: a classifier decides what is safe; you are asked only when it is unsure."
          : "Ask mode: you approve every gated action.",
      );
    } catch (e) {
      fail(e);
    }
  }, [conn, mode, fail]);

  const builtins: SlashCommand[] = useMemo(
    () => [
      { name: "new", description: "start a new conversation", kind: "builtin" },
      { name: "rewind", description: "go back to an earlier message", kind: "builtin" },
      { name: "branches", description: "draw this conversation's branches", kind: "builtin" },
      { name: "share", description: "name this point so another chat can open it", kind: "builtin" },
      { name: "mode", description: "approval mode: /mode auto or /mode ask", kind: "builtin" },
      { name: "model", description: "show or switch the model: /model provider:name", kind: "builtin" },
      { name: "compact", description: "summarise the conversation to free context: /compact [focus]", kind: "builtin" },
    ],
    [],
  );

  const restored = useRef(false);
  useEffect(() => {
    if (!conn) return;
    void refreshSessions().catch(fail);
    if (restored.current) return;
    restored.current = true;
    if (wanted.current) void openSession(wanted.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conn]);

  const openSession = useCallback(
    async (id: string) => {
      if (!conn) return;
      setDrawer(false);
      setError("");
      live.detach();
      setPendingUser(null);
      setStopping(false);
      setActiveId(id);
      activeRef.current = id;
      try {
        await loadHistory(id);
        const run = runBySession.current.get(id);
        if (run) live.follow(run); // a run that kept going while we looked elsewhere
      } catch (e) {
        fail(e);
      }
    },
    [conn, live, loadHistory, fail],
  );

  const newConversation = useCallback(() => {
    live.detach();
    setActiveId(null);
    activeRef.current = null;
    setHistory([]);
    setPendingUser(null);
    setForkable(null);
    setDrawer(false);
  }, [live]);

  const send = useCallback(
    async (text: string) => {
      if (!conn) return;
      setError("");
      const working = activeRef.current ? runBySession.current.get(activeRef.current) : undefined;
      if (running && working) {
        try {
          const { accepted } = await api.input(conn, working, text);
          if (accepted) setQueued((q) => [...q, text]);
          else {
            draftCount.current += 1;
            setDraft({ text, n: draftCount.current });
            setNotice("The run had just finished; your message is back in the box to send.");
          }
        } catch (e) {
          fail(e);
        }
        return;
      }
      try {
        let id = activeRef.current;
        if (!id) {
          id = (await api.newSession(conn)).id;
          setActiveId(id);
          activeRef.current = id;
          setHistory([]);
        }
        setForkable(null);
        setPendingUser(text);
        const { run_id } = await api.send(conn, id, text);
        runBySession.current.set(id, run_id);
        live.follow(run_id);
        void refreshSessions().catch(() => undefined);
      } catch (e) {
        setPendingUser(null);
        fail(e);
      }
    },
    [conn, live, running, refreshSessions, fail],
  );

  // Queued messages the model has read now show in the timeline; the rest stay "queued".
  const unread = useMemo(() => {
    const left = queued.slice();
    for (const item of live.state.items) {
      const at = item.kind === "user" ? left.indexOf(item.text) : -1;
      if (at >= 0) left.splice(at, 1);
    }
    return left;
  }, [queued, live.state.items]);
  const unreadRef = useRef(unread);

  // Take a queued message back before the model reads it: into the composer to edit, or gone.
  const withdraw = useCallback(
    async (text: string, edit: boolean) => {
      const working = activeRef.current ? runBySession.current.get(activeRef.current) : undefined;
      if (!conn || !working) return;
      try {
        const { withdrawn } = await api.withdraw(conn, working, text);
        if (!withdrawn) {
          setNotice("The agent has already read that message.");
          return;
        }
        setQueued((q) => {
          const at = q.lastIndexOf(text);
          return at < 0 ? q : [...q.slice(0, at), ...q.slice(at + 1)];
        });
        if (edit) {
          draftCount.current += 1;
          setDraft({ text, n: draftCount.current });
        }
      } catch (e) {
        fail(e);
      }
    },
    [conn, fail],
  );
  useEffect(() => {
    unreadRef.current = unread;
  }, [unread]);

  const onRewound = useCallback(
    (result: RewindResult) => {
      setHistoryTab(null);
      const moved = result.files.moved.length;
      const left = result.files.conflicts;
      setNotice(
        `Rewound.${moved ? ` ${moved} file${moved === 1 ? "" : "s"} restored.` : ""}` +
          (left.length ? ` Left alone (changed since): ${left.join(", ")}.` : ""),
      );
      void (async () => {
        try {
          await refreshSessions();
          if (result.session_id !== activeRef.current) await openSession(result.session_id);
          else await loadHistory(result.session_id);
          draftCount.current += 1;
          setDraft({ text: result.prompt, n: draftCount.current }); // the message, to edit and resend
        } catch (e) {
          fail(e);
        }
      })();
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [refreshSessions, loadHistory, fail],
  );

  const stop = useCallback(async () => {
    const run = live.rootRun.current;
    if (!conn || !run || stopping) return;
    setStopping(true);
    try {
      await api.cancel(conn, run);
    } catch (e) {
      setStopping(false);
      fail(e);
    }
  }, [conn, live.rootRun, stopping, fail]);

  const decide = useCallback(
    async (tool: Tool, approve: boolean) => {
      if (!conn || !tool.approval) return;
      try {
        await api.decide(conn, tool.runId, tool.approval.id, approve);
      } catch (e) {
        fail(e);
      }
    },
    [conn, fail],
  );

  const pendingApproval = useMemo(
    () => live.state.items.find((i): i is Tool => i.kind === "tool" && i.approval?.state === "pending") ?? null,
    [live.state.items],
  );

  useEffect(() => {
    if (!pendingApproval) return;
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "TEXTAREA" || target.tagName === "INPUT")) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "y") void decide(pendingApproval, true);
      if (e.key === "n") void decide(pendingApproval, false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [pendingApproval, decide]);

  // Keep the newest content in view, unless the reader scrolled up to look at something.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (nearBottom) el.scrollTo({ top: el.scrollHeight });
  }, [live.state.items, history, pendingUser]);

  if (!ready) return null;
  if (!conn) {
    return (
      <Connect
        initialError={connectError}
        onConnected={(c) => {
          saveConn(c);
          setConnectError("");
          setConn(c);
        }}
      />
    );
  }

  const openFile = (path: string) => {
    setFocusPath(path);
    setPanelOpen(true);
  };

  const activeTitle = sessions.find((s) => s.id === activeId)?.title ?? "New conversation";
  const lastAssistant = (() => {
    for (let i = history.length - 1; i >= 0; i -= 1) if (history[i]?.kind === "assistant") return i;
    return -1;
  })();

  return (
    <div className={`grid h-full grid-cols-1 ${panelOpen ? "md:grid-cols-[300px_minmax(0,1fr)_minmax(380px,520px)]" : "md:grid-cols-[300px_minmax(0,1fr)]"}`}>
      <div className={`${drawer ? "fixed inset-0 z-20 w-[300px] max-w-[85vw]" : "hidden"} min-h-0 md:static md:block md:w-auto`}>
        <Sidebar
          sessions={sessions}
          activeId={activeId}
          busyIds={new Set(runBySession.current.keys())}
          onSelect={(id) => void openSession(id)}
          onNew={newConversation}
          onRename={(id, title) => void api.rename(conn, id, title).then(refreshSessions).catch(fail)}
          onDelete={(id) =>
            void api
              .remove(conn, id)
              .then(() => {
                if (id === activeId) newConversation();
                return refreshSessions();
              })
              .catch(fail)
          }
          onDisconnect={() => {
            live.detach();
            saveConn(null);
            setConn(null);
          }}
        />
      </div>
      {drawer && <button className="fixed inset-0 z-10 bg-ink/40 md:hidden" aria-label="Close menu" onClick={() => setDrawer(false)} />}

      <main id="conversation" tabIndex={-1} className="flex h-full min-h-0 min-w-0 flex-col outline-none">
        <header className="flex items-center gap-3 border-b-2 border-line bg-surface px-4 py-3">
          <button type="button" className="btn md:hidden" onClick={() => setDrawer(true)} aria-label="Open conversations">
            ☰
          </button>
          <Mark kind={running ? "running" : live.state.phase === "failed" ? "failed" : "idle"} />
          <h2 className="min-w-0 flex-1 truncate text-lg font-bold">{activeTitle}</h2>
          {changes.files.length > 0 && (
            <button type="button" className={`btn ${panelOpen ? "btn-yellow" : ""}`} onClick={() => setPanelOpen((v) => !v)} aria-pressed={panelOpen} aria-label={`Files changed: ${changes.files.length}`}>
              Files <span className="code tabular-nums">{changes.files.length}</span>
            </button>
          )}
          <button
            type="button"
            className={`btn ${mode === "auto" ? "btn-yellow" : ""}`}
            onClick={() => void toggleMode()}
            aria-pressed={mode === "auto"}
            title="Who approves tool calls: you (ask), or a classifier that asks you when unsure (auto)"
          >
            {mode === "auto" ? "Auto mode" : "Ask mode"}
          </button>
          {activeId && !running && (
            <button type="button" className="btn" onClick={() => setHistoryTab("rewind")} aria-label="History: rewind, branches, share">
              History
            </button>
          )}
          {usage && usage.calls > 0 && <CostChip usage={usage} />}
          <span className="code text-soft" aria-live="polite">
            {stopping ? "stopping…" : running ? (pendingApproval ? "waiting for you" : "running") : live.state.phase === "cancelled" ? "stopped" : ""}
          </span>
          <ThemeToggle />
        </header>

        {usage && usage.calls > 0 && (
          <div className="border-b-2 border-line bg-sunk px-4 py-1 sm:hidden">
            <CostChip usage={usage} always />
          </div>
        )}

        {filesDiffer && activeId && (
          <div role="status" className="flex items-center gap-3 border-b-2 border-line bg-yellow px-4 py-2 font-bold text-on-yellow">
            <span className="flex-1">The files in the folder match another conversation.</span>
            <button
              className="btn"
              onClick={() =>
                void api
                  .filesRestore(conn, activeId)
                  .then((r) => {
                    setNotice(`${r.moved.length} file${r.moved.length === 1 ? "" : "s"} restored.${r.conflicts.length ? ` Left alone (changed since): ${r.conflicts.join(", ")}.` : ""}`);
                    setFilesDiffer(r.conflicts.length > 0);
                  })
                  .catch(fail)
              }
            >
              Put this conversation&apos;s files in the folder
            </button>
          </div>
        )}

        {notice && (
          <div role="status" className="flex items-center gap-3 border-b-2 border-line bg-sunk px-4 py-2">
            <span className="flex-1">{notice}</span>
            <button className="underline" onClick={() => setNotice("")}>
              dismiss
            </button>
          </div>
        )}

        {error && (
          <div role="alert" className="flex items-center gap-3 border-b-2 border-line bg-red px-4 py-2 font-bold text-on-accent">
            <span className="flex-1">{error}</span>
            <button className="underline" onClick={() => setError("")}>
              dismiss
            </button>
          </div>
        )}

        <div ref={scroller} className="contain min-h-0 flex-1 overflow-y-auto px-4 py-6" role="log" aria-label="Conversation">
          <div className="mx-auto max-w-3xl space-y-4">
            {history.length === 0 && !pendingUser && live.state.items.length === 0 && <EmptyState />}
            {history.map((row, i) =>
              row.kind === "user" ? (
                <UserBubble key={`h${i}`} text={row.text} />
              ) : row.kind === "compaction" ? (
                <details key={`h${i}`} className="code text-soft">
                  <summary>↺ context compacted: earlier messages were summarised</summary>
                  <p className="whitespace-pre-wrap">{row.text}</p>
                </details>
              ) : row.kind === "assistant" ? (
                <div key={`h${i}`} className="space-y-2">
                  <AssistantBubble text={row.text} />
                  {i === lastAssistant && forkable?.sessionId === activeId && (
                    <button
                      type="button"
                      className="btn btn-ghost"
                      onClick={() =>
                        void api
                          .branch(conn, forkable.sessionId, forkable.eventId)
                          .then(async ({ id }) => {
                            await refreshSessions();
                            await openSession(id);
                          })
                          .catch(fail)
                      }
                    >
                      ⑂ Fork a branch from here
                    </button>
                  )}
                </div>
              ) : (
                <ToolCard key={`h${i}`} tool={historyTool(row, i)} onOpenFile={openFile} />
              ),
            )}
            {pendingUser && <UserBubble text={pendingUser} />}
            {live.state.items.map((item) => {
              switch (item.kind) {
                case "tool":
                  return <ToolCard key={item.key} tool={item} onDecide={(t, a) => void decide(t, a)} onOpenFile={openFile} />;
                case "assistant":
                  return <AssistantBubble key={item.key} text={item.text} depth={item.depth} />;
                case "reasoning":
                  return <ReasoningCard key={item.key} item={item} />;
                case "subagent":
                  return <SubagentCard key={item.key} item={item} />;
                case "failure":
                  return <FailureCard key={item.key} text={item.text} />;
                case "notice":
                  return <p key={item.key} className="code text-soft">↺ {item.text}</p>;
                case "user":
                  return <UserBubble key={item.key} text={item.text} />;
              }
            })}
            {unread.map((text, i) => (
              <UserBubble
                key={`q${i}`}
                text={text}
                queued
                onEdit={() => void withdraw(text, true)}
                onCancel={() => void withdraw(text, false)}
              />
            ))}
            {live.state.phase === "cancelled" && <p className="code text-soft">■ stopped. Nothing is running and no further calls will be made.</p>}
          </div>
        </div>

        <div className="border-t-2 border-line bg-paper px-4 pt-4 pb-[max(1rem,env(safe-area-inset-bottom))]">
          <div className="mx-auto max-w-3xl">
            <Composer
              running={running}
              stopping={stopping}
              disabled={false}
              draft={draft}
              commands={[...builtins, ...skills.filter((s) => !builtins.some((b) => b.name === s.name))]}
              onCommand={(name, argument) => {
                if (name === "new") newConversation();
                else if (name === "rewind" && activeId) setHistoryTab("rewind");
                else if (name === "branches" && activeId) setHistoryTab("branches");
                else if (name === "share" && activeId) setHistoryTab("share");
                else if (name === "mode") {
                  if (argument === "auto" || argument === "ask") {
                    void api.setApprovalMode(conn, argument).then((r) => setMode(r.mode)).catch(fail);
                  } else void toggleMode();
                } else if (name === "model") {
                  const switching = argument ? api.setModel(conn, argument) : api.model(conn);
                  void switching
                    .then((r) => setNotice(argument ? `Model: ${r.model} (from the next message on).` : `Model: ${r.model}. Switch: /model provider:name`))
                    .catch(fail);
                } else if (name === "compact" && activeId) {
                  setNotice("Compacting the conversation…");
                  void api
                    .compact(conn, activeId, argument)
                    .then((r) => setNotice(r.messages ? `Compacted ${r.messages} messages into a summary; the next message starts from it.` : "Nothing new to compact."))
                    .catch(fail);
                } else setNotice("Open a conversation first.");
              }}
              onSend={(t) => void send(t)}
              onStop={() => void stop()}
            />
          </div>
        </div>
      </main>

      {historyTab && activeId && (
        <HistoryPanel
          conn={conn}
          sessionId={activeId}
          initialTab={historyTab}
          onClose={() => setHistoryTab(null)}
          onRewound={onRewound}
          onOpened={(id) => {
            setHistoryTab(null);
            setNotice("Opened a new conversation from that point. If its files are not in the folder yet, use the banner above.");
            void refreshSessions().then(() => openSession(id)).catch(fail);
          }}
          onError={fail}
        />
      )}

      {panelOpen && activeId && (
        <div className="fixed inset-0 z-30 min-h-0 md:static md:z-auto">
          <ChangesPanel
            conn={conn}
            sessionId={activeId}
            files={changes.files}
            note={changes.note}
            focusPath={focusPath}
            refreshKey={changeTick + (running ? 0 : 1)}
            onClose={() => setPanelOpen(false)}
          />
        </div>
      )}
    </div>
  );
}

function CostChip({ usage, always = false }: { usage: UsageInfo; always?: boolean }) {
  const unpriced = usage.calls - usage.priced_calls;
  const cost = usage.priced_calls > 0 ? formatCost(usage.cost_usd) : "cost n/a";
  const over = usage.budget_usd !== null && usage.cost_usd >= usage.budget_usd;
  const detail =
    `${usage.calls} model calls · ${usage.input_tokens.toLocaleString()} tokens in · ` +
    `${usage.output_tokens.toLocaleString()} out` +
    (usage.reasoning_tokens ? ` (${usage.reasoning_tokens.toLocaleString()} reasoning)` : "") +
    (unpriced > 0 ? ` · ${unpriced} unpriced` : "") +
    (usage.models_summary ? ` · ${usage.models_summary}` : "") +
    (usage.budget_usd !== null ? ` · budget ${formatCost(usage.budget_usd)}` : "");
  return (
    <span
      className={`code border-2 border-line px-2 py-0.5 tabular-nums ${always ? "inline-block" : "hidden sm:inline"} ${over ? "bg-red text-on-accent" : "bg-sunk"}`}
      title={detail}
      aria-label={`Conversation cost: ${detail}`}
    >
      {cost}
      {unpriced > 0 && usage.priced_calls > 0 ? "+" : ""} · {formatTokens(usage.input_tokens + usage.output_tokens)} tok
      {usage.budget_usd !== null ? ` / ${formatCost(usage.budget_usd)}` : ""}
    </span>
  );
}

function EmptyState() {
  return (
    <div className="box mx-auto mt-10 max-w-md space-y-3 p-6">
      <h3 className="text-balance text-2xl font-bold tracking-tight">What should it do?</h3>
      <p className="text-soft">
        Every tool call shows up here as it happens. Risky ones (shell commands, file writes) wait for you to approve. The red{" "}
        <b>Stop</b> button cancels the run and any subagent it started, so nothing keeps spending.
      </p>
    </div>
  );
}

function ThemeToggle() {
  const [theme, setTheme] = useState<"system" | "light" | "dark">("system");
  useEffect(() => {
    try {
      const saved = localStorage.getItem("agent-stack:theme");
      if (saved === "light" || saved === "dark") setTheme(saved);
    } catch {
      // ignore: the system theme is used
    }
  }, []);
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
    try {
      if (theme === "system") localStorage.removeItem("agent-stack:theme");
      else localStorage.setItem("agent-stack:theme", theme);
    } catch {
      // ignore
    }
  }, [theme]);
  const next = theme === "system" ? "light" : theme === "light" ? "dark" : "system";
  return (
    <button type="button" className="btn btn-ghost" onClick={() => setTheme(next)} aria-label={`Theme: ${theme}. Switch to ${next}.`}>
      {theme === "dark" ? "☾" : theme === "light" ? "☀" : "◐"}
    </button>
  );
}
