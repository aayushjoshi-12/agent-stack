import type { ReactNode } from "react";

// A deliberately small renderer for what models actually write: paragraphs, fenced code,
// inline code and bold. It builds React nodes (never raw HTML), so model output cannot inject markup.

function inline(text: string, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /(\*\*[^\n]+?\*\*|`[^`\n]+`)/g;
  let last = 0;
  let i = 0;
  for (const match of text.matchAll(pattern)) {
    const at = match.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const token = match[0];
    out.push(
      token.startsWith("`") ? (
        <code key={`${keyBase}-${i}`} className="code bg-sunk px-1 py-0.5 border border-line/30">
          {token.slice(1, -1)}
        </code>
      ) : (
        // bold may wrap inline code (**`path`**): render the inside too, not as literal backticks
        <strong key={`${keyBase}-${i}`}>{inline(token.slice(2, -2), `${keyBase}-${i}b`)}</strong>
      ),
    );
    last = at + token.length;
    i += 1;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ text }: { text: string }) {
  const parts = text.split(/```[ \t]*[\w+-]*\n?([\s\S]*?)```/g);
  return (
    <div className="space-y-3 break-words">
      {parts.map((part, index) =>
        index % 2 === 1 ? (
          <pre key={index} className="code box-flat overflow-x-auto bg-sunk p-3 whitespace-pre">
            {part.replace(/^\s*\n/, "").replace(/\s+$/, "")}
          </pre>
        ) : (
          part
            .split(/\n{2,}/)
            .filter((p) => p.trim())
            .map((paragraph, j) => (
              <p key={`${index}-${j}`} className="whitespace-pre-wrap">
                {inline(paragraph, `${index}-${j}`)}
              </p>
            ))
        ),
      )}
    </div>
  );
}
