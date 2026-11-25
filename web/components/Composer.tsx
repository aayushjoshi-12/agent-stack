"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { SlashCommand } from "@/lib/api";

export function Composer({
  running,
  stopping,
  disabled,
  draft,
  commands,
  onCommand,
  onSend,
  onStop,
}: {
  running: boolean;
  stopping: boolean;
  disabled: boolean;
  /** Text to put in the box (a rewound message to edit, or "" to clear it); `n` makes each one new. */
  draft?: { text: string; n: number } | null;
  /** Every `/command` offered while typing: the page's built-ins and the skills. */
  commands: SlashCommand[];
  /** A built-in was chosen (typed in full or picked from the list). */
  onCommand: (name: string, argument: string) => void;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const area = useRef<HTMLTextAreaElement>(null);
  const [picked, setPicked] = useState(0);
  const [dismissed, setDismissed] = useState(false);

  // The list shows while a command name is being typed: "/" then letters, no space yet.
  const matches = useMemo(() => {
    if (!/^\/[\w-]*$/.test(text) || dismissed) return [];
    const q = text.slice(1).toLowerCase();
    const starts = commands.filter((c) => c.name.toLowerCase().startsWith(q));
    const rest = commands.filter((c) => !starts.includes(c) && c.name.toLowerCase().includes(q));
    return [...starts, ...rest].slice(0, 30);
  }, [text, commands, dismissed]);
  useEffect(() => setPicked(0), [text]);
  useEffect(() => setDismissed(false), [text]);

  const accept = (c: SlashCommand) => {
    setText(`/${c.name} `);
    area.current?.focus();
  };

  useEffect(() => {
    if (!draft) return;
    setText(draft.text);
    area.current?.focus();
  }, [draft]);

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [text]);

  const send = () => {
    const value = text.trim();
    if (!value || running || disabled) return;
    const typed = /^\/([A-Za-z][\w-]*)(?:\s+(.*))?$/s.exec(value);
    const word = typed?.[1]?.toLowerCase();
    const builtin = typed && word ? commands.find((c) => c.kind === "builtin" && c.name === word) : undefined;
    setText("");
    if (typed && builtin) onCommand(builtin.name, (typed[2] ?? "").trim());
    else onSend(value);
  };

  return (
    <form
      className="box relative flex items-end gap-3 p-3 focus-within:outline-3 focus-within:outline-offset-2 focus-within:outline-blue"
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      {matches.length > 0 && (
        <ul
          role="listbox"
          aria-label="Commands"
          className="box absolute right-0 bottom-full left-0 z-20 mb-2 max-h-72 overflow-y-auto bg-surface p-1"
        >
          {matches.map((c, i) => (
            <li key={c.name} role="option" aria-selected={i === picked}>
              <button
                type="button"
                className={`flex w-full items-baseline gap-3 px-2 py-1 text-left ${i === picked ? "bg-yellow text-on-yellow" : ""}`}
                onMouseDown={(e) => {
                  e.preventDefault();
                  accept(c);
                }}
              >
                <span className="code font-bold">/{c.name}</span>
                <span className="min-w-0 flex-1 truncate text-soft">{c.description}</span>
                {c.kind === "skill" && <span className="code text-soft">skill</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
      <label htmlFor="composer" className="sr-only">
        Message the agent
      </label>
      <textarea
        id="composer"
        name="message"
        autoComplete="off"
        ref={area}
        value={text}
        rows={1}
        disabled={disabled}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (matches.length > 0 && !e.nativeEvent.isComposing) {
            const chosen = matches[picked];
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              setPicked((i) => (i + (e.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length);
              return;
            }
            if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && chosen && text.trim() !== `/${chosen.name}`)) {
              e.preventDefault();
              if (chosen) accept(chosen);
              return;
            }
            if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              setDismissed(true);
              return;
            }
          }
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            send();
          } else if (e.key === "Escape" && running) {
            onStop();
          }
        }}
        placeholder={running ? "Working… press Esc or Stop to cancel" : "Ask the agent to do something…"}
        className="min-h-10 flex-1 resize-none bg-transparent py-2 outline-none placeholder:text-soft"
      />
      {running ? (
        <button type="button" className="btn btn-red" onClick={onStop} disabled={stopping} aria-label="Stop the run and every subagent">
          <span className="inline-block h-3 w-3 bg-on-accent" aria-hidden />
          {stopping ? "Stopping…" : "Stop"}
        </button>
      ) : (
        <button type="submit" className="btn btn-blue" disabled={disabled || !text.trim()}>
          Send
        </button>
      )}
    </form>
  );
}
