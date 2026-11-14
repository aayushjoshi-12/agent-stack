"use client";

import { useEffect, useRef, useState } from "react";

export function Composer({
  running,
  stopping,
  disabled,
  onSend,
  onStop,
}: {
  running: boolean;
  stopping: boolean;
  disabled: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const area = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = area.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [text]);

  const send = () => {
    const value = text.trim();
    if (!value || running || disabled) return;
    onSend(value);
    setText("");
  };

  return (
    <form
      className="box flex items-end gap-3 p-3"
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      <label htmlFor="composer" className="sr-only">
        Message the agent
      </label>
      <textarea
        id="composer"
        ref={area}
        value={text}
        rows={1}
        disabled={disabled}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            send();
          } else if (e.key === "Escape" && running) {
            onStop();
          }
        }}
        placeholder={running ? "Working… press Esc or Stop to cancel" : "Ask the agent to do something"}
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
