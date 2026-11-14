// Geometric status marks (ADR 013): circle = running, square = done, triangle = needs you,
// crossed square = failed. Shape carries the meaning, colour only reinforces it, so the marks
// stay legible without colour vision.
export type MarkKind = "running" | "ok" | "failed" | "approval" | "idle";

const LABEL: Record<MarkKind, string> = {
  running: "running",
  ok: "done",
  failed: "failed",
  approval: "waiting for approval",
  idle: "idle",
};

export function Mark({ kind, size = 14 }: { kind: MarkKind; size?: number }) {
  const common = { width: size, height: size, viewBox: "0 0 14 14", role: "img", "aria-label": LABEL[kind] } as const;
  switch (kind) {
    case "running":
      return (
        <svg {...common} className="pulse shrink-0">
          <circle cx="7" cy="7" r="6" fill="var(--blue)" stroke="var(--line)" strokeWidth="1.5" />
        </svg>
      );
    case "ok":
      return (
        <svg {...common} className="shrink-0">
          <rect x="1.5" y="1.5" width="11" height="11" fill="var(--ink)" />
        </svg>
      );
    case "failed":
      return (
        <svg {...common} className="shrink-0">
          <rect x="1" y="1" width="12" height="12" fill="var(--red)" stroke="var(--line)" strokeWidth="1.5" />
          <path d="M4 4l6 6M10 4l-6 6" stroke="var(--on-accent)" strokeWidth="1.8" />
        </svg>
      );
    case "approval":
      return (
        <svg {...common} className="pulse shrink-0">
          <path d="M7 1.2l6.2 11H0.8z" fill="var(--yellow)" stroke="var(--line)" strokeWidth="1.5" strokeLinejoin="round" />
        </svg>
      );
    default:
      return (
        <svg {...common} className="shrink-0">
          <circle cx="7" cy="7" r="5.5" fill="none" stroke="var(--ink-soft)" strokeWidth="1.5" />
        </svg>
      );
  }
}
