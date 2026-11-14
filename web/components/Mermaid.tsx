"use client";

import { useEffect, useId, useState } from "react";

const MAX_CHART_CHARS = 20_000;

type Theme = "light" | "dark";

function currentTheme(): Theme {
  const forced = document.documentElement.getAttribute("data-theme");
  if (forced === "light" || forced === "dark") return forced;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** Re-renders when the page theme changes (the toggle, or the system setting). */
function useTheme(): Theme {
  const [theme, setTheme] = useState<Theme>("light");
  useEffect(() => {
    const update = () => setTheme(currentTheme());
    update();
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", update);
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      media.removeEventListener("change", update);
      observer.disconnect();
    };
  }, []);
  return theme;
}

/**
 * A mermaid diagram, rendered in the browser. The library is loaded only when a diagram first
 * appears (it is large). `securityLevel: "strict"` makes mermaid sanitise labels and disable
 * click handlers, so text written by a model cannot run script. A diagram that does not parse
 * (common) falls back to its source with the error, never to a broken page.
 */
export function Mermaid({ chart }: { chart: string }) {
  const theme = useTheme();
  const id = `mmd-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const [svg, setSvg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showSource, setShowSource] = useState(false);
  const source = chart.trim();
  const tooBig = source.length > MAX_CHART_CHARS;

  useEffect(() => {
    if (tooBig) return;
    let current = true;
    void (async () => {
      try {
        const { default: mermaid } = await import("mermaid");
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          theme: theme === "dark" ? "dark" : "neutral",
          fontFamily: "var(--font-grotesk), system-ui, sans-serif",
          maxTextSize: MAX_CHART_CHARS,
        });
        const rendered = await mermaid.render(id, source);
        if (current) {
          setSvg(rendered.svg);
          setError(null);
        }
      } catch (e) {
        if (current) {
          setSvg(null);
          setError(e instanceof Error ? e.message.split("\n")[0] ?? "invalid diagram" : "invalid diagram");
        }
        document.getElementById(`d${id}`)?.remove(); // mermaid leaves an error node behind
      }
    })();
    return () => {
      current = false;
    };
  }, [source, theme, id, tooBig]);

  const asSource = showSource || error !== null || tooBig;
  return (
    <figure className="box-flat my-3 overflow-hidden" aria-label="Diagram">
      <figcaption className="flex items-center gap-3 border-b-2 border-line bg-sunk px-3 py-1 text-xs font-bold uppercase tracking-wide">
        <span className="mr-auto">mermaid</span>
        {error && <span className="text-red normal-case">could not render: {error}</span>}
        {tooBig && <span className="text-red normal-case">diagram too large to draw</span>}
        {!error && !tooBig && (
          <button type="button" className="underline decoration-dotted underline-offset-4" onClick={() => setShowSource((v) => !v)}>
            {showSource ? "show diagram" : "show source"}
          </button>
        )}
      </figcaption>
      {asSource ? (
        <pre className="code overflow-x-auto p-3 whitespace-pre">{source}</pre>
      ) : svg ? (
        <div className="overflow-x-auto bg-surface p-3 [&_svg]:mx-auto [&_svg]:h-auto [&_svg]:max-w-full" role="img" aria-label="Rendered diagram. Choose show source for its text." dangerouslySetInnerHTML={{ __html: svg }} />
      ) : (
        <p className="code p-3 text-soft">drawing the diagram…</p>
      )}
    </figure>
  );
}
