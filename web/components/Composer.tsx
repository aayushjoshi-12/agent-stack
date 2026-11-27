"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { SlashCommand } from "@/lib/api";
import { type Attachment, IMAGE_TYPES, imageProblem, MAX_IMAGES, readImage } from "@/lib/images";

export function Composer({
  running,
  stopping,
  disabled,
  draft,
  commands,
  onCommand,
  onSend,
  onStop,
  onNotice,
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
  onSend: (text: string, images: Attachment[]) => void;
  onStop: () => void;
  /** A file that cannot be attached: say why. */
  onNotice: (text: string) => void;
}) {
  const [text, setText] = useState("");
  const [images, setImages] = useState<Attachment[]>([]);
  const [dragging, setDragging] = useState(false);
  const picker = useRef<HTMLInputElement>(null);

  // Picked, pasted or dropped files: images are attached, anything else is refused with a reason.
  const attach = (files: Iterable<File>) => {
    for (const file of files) {
      const problem = imageProblem(file);
      if (problem) {
        onNotice(problem);
        continue;
      }
      void readImage(file)
        .then((image) =>
          setImages((now) => {
            if (now.length < MAX_IMAGES) return [...now, image];
            onNotice(`At most ${MAX_IMAGES} images per message.`);
            return now;
          }),
        )
        .catch((e: Error) => onNotice(e.message));
    }
  };
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
    if ((!value && images.length === 0) || disabled) return;
    if (running && images.length > 0) return; // a queued message is text only: send it after the run
    const typed = /^\/([A-Za-z][\w-]*)(?:\s+(.*))?$/s.exec(value);
    if (running && typed) return; // a command waits for the run; plain text is queued into it
    const word = typed?.[1]?.toLowerCase();
    const builtin = typed && word ? commands.find((c) => c.kind === "builtin" && c.name === word) : undefined;
    setText("");
    if (typed && builtin && images.length === 0) onCommand(builtin.name, (typed[2] ?? "").trim());
    else {
      onSend(value, images);
      setImages([]); // the page owns the previews now (the bubble shows them)
    }
  };

  return (
    <form
      className={`box relative flex flex-wrap items-end gap-3 p-3 focus-within:outline-3 focus-within:outline-offset-2 focus-within:outline-blue ${dragging ? "outline-3 outline-offset-2 outline-blue" : ""}`}
      onSubmit={(e) => {
        e.preventDefault();
        send();
      }}
      onDragOver={(e) => {
        if (!e.dataTransfer.types.includes("Files")) return;
        e.preventDefault();
        setDragging(true);
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={(e) => {
        if (!e.dataTransfer.files.length) return;
        e.preventDefault();
        setDragging(false);
        attach(Array.from(e.dataTransfer.files));
      }}
    >
      {images.length > 0 && (
        <ul aria-label="Attached images" className="flex w-full flex-wrap gap-2">
          {images.map((image, i) => (
            <li key={image.url} className="relative">
              <img src={image.url} alt={image.name} title={image.name} className="h-16 w-16 border-2 border-line bg-surface object-cover" />
              <button
                type="button"
                className="absolute -top-2 -right-2 h-6 w-6 border-2 border-line bg-surface text-xs font-bold leading-none"
                aria-label={`Remove ${image.name}`}
                onClick={() => {
                  URL.revokeObjectURL(image.url);
                  setImages((now) => now.filter((_, at) => at !== i));
                }}
              >
                x
              </button>
            </li>
          ))}
        </ul>
      )}
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
                <span className={`min-w-0 flex-1 truncate ${i === picked ? "" : "text-soft"}`}>{c.description}</span>
                {c.kind === "skill" && <span className={`code ${i === picked ? "" : "text-soft"}`}>skill</span>}
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
        onPaste={(e) => {
          const files = Array.from(e.clipboardData.files).filter((f) => f.type.startsWith("image/"));
          if (files.length === 0) return; // plain text pastes as usual
          e.preventDefault();
          attach(files);
        }}
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
        placeholder={running ? "Working… type to queue a message, Esc or Stop to cancel" : "Ask the agent to do something…"}
        className="min-h-10 flex-1 resize-none bg-transparent py-2 outline-none placeholder:text-soft"
      />
      <input
        ref={picker}
        type="file"
        accept={IMAGE_TYPES.join(",")}
        multiple
        className="hidden"
        onChange={(e) => {
          attach(Array.from(e.target.files ?? []));
          e.target.value = ""; // the same file can be picked again
        }}
      />
      <button
        type="button"
        className="btn btn-ghost"
        onClick={() => picker.current?.click()}
        disabled={disabled || running}
        aria-label="Attach an image"
        title={running ? "Images go with a new message: send it when the run finishes" : "Attach an image (or paste or drop one)"}
      >
        Image
      </button>
      {running && text.trim() && (
        <button type="submit" className="btn btn-blue" disabled={disabled}>
          Queue
        </button>
      )}
      {running ? (
        <button type="button" className="btn btn-red" onClick={onStop} disabled={stopping} aria-label="Stop the run and every subagent">
          <span className="inline-block h-3 w-3 bg-on-accent" aria-hidden />
          {stopping ? "Stopping…" : "Stop"}
        </button>
      ) : (
        <button type="submit" className="btn btn-blue" disabled={disabled || (!text.trim() && images.length === 0)}>
          Send
        </button>
      )}
    </form>
  );
}
