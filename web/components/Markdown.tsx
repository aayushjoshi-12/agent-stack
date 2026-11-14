"use client";

import { Children, isValidElement, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { Mermaid } from "./Mermaid";

/** The plain text inside a rendered node (highlighting wraps code in many spans). */
function textOf(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(textOf).join("");
  if (isValidElement(node)) return textOf((node.props as { children?: ReactNode }).children);
  return "";
}

function CodeBlock({ lang, raw, children }: { lang: string | undefined; raw: string; children: ReactNode }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(raw);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard can be unavailable (insecure context); the text stays selectable
    }
  };
  return (
    <div className="box-flat my-3 overflow-hidden">
      <div className="flex items-center gap-3 border-b-2 border-line bg-sunk px-3 py-1 text-xs font-bold uppercase tracking-wide">
        <span className="mr-auto">{lang ?? "text"}</span>
        <button type="button" onClick={() => void copy()} className="underline decoration-dotted underline-offset-4" aria-label="Copy code">
          {copied ? "copied" : "copy"}
        </button>
      </div>
      <pre className="code overflow-x-auto p-3 whitespace-pre">{children}</pre>
    </div>
  );
}

// Everything a model writes is untrusted. react-markdown renders to React nodes (never raw
// HTML) and rewrites unsafe link schemes; on top of that, remote images are NOT loaded (a
// prompt-injected image URL could carry data out), and links open in a new tab without a referrer.
const components: Components = {
  h1: ({ children }) => <h1 className="mt-5 mb-2 text-balance text-2xl font-bold tracking-tight">{children}</h1>,
  h2: ({ children }) => <h2 className="mt-5 mb-2 text-balance text-xl font-bold tracking-tight">{children}</h2>,
  h3: ({ children }) => <h3 className="mt-4 mb-1 text-balance text-lg font-bold">{children}</h3>,
  h4: ({ children }) => <h4 className="mt-3 mb-1 font-bold">{children}</h4>,
  p: ({ children }) => <p className="my-2 break-words">{children}</p>,
  ul: ({ children }) => <ul className="my-2 list-disc space-y-1 pl-6">{children}</ul>,
  ol: ({ children }) => <ol className="my-2 list-decimal space-y-1 pl-6">{children}</ol>,
  li: ({ children }) => <li className="break-words">{children}</li>,
  blockquote: ({ children }) => <blockquote className="my-3 border-l-4 border-line bg-sunk px-4 py-1 text-soft">{children}</blockquote>,
  hr: () => <hr className="my-4 border-t-2 border-line" />,
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer nofollow" className="font-bold text-blue underline underline-offset-2">
      {children}
    </a>
  ),
  img: ({ src, alt }) => (
    <span className="code box-flat inline-block px-2 py-0.5 text-soft" title={typeof src === "string" ? src : undefined}>
      [image not loaded{alt ? `: ${alt}` : ""}]
    </span>
  ),
  table: ({ children }) => (
    <div className="my-3 overflow-x-auto">
      <table className="box-flat w-full border-collapse text-left text-sm">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-sunk">{children}</thead>,
  th: ({ children }) => <th className="border-2 border-line px-3 py-1.5 font-bold">{children}</th>,
  td: ({ children }) => <td className="border-2 border-line px-3 py-1.5 align-top">{children}</td>,
  input: (props) => <input {...props} disabled className="mr-2 align-middle" />,
  code: ({ className, children }) => (
    <code className={`${className ?? ""} ${className ? "" : "code bg-sunk px-1 py-0.5 border border-line/30"}`.trim()}>{children}</code>
  ),
  pre: ({ children }) => {
    const child = Children.toArray(children)[0];
    if (isValidElement(child)) {
      const props = child.props as { className?: string; children?: ReactNode };
      const lang = /language-([\w+-]+)/.exec(props.className ?? "")?.[1];
      const raw = textOf(props.children).replace(/\n$/, "");
      if (lang === "mermaid") return <Mermaid chart={raw} />;
      return (
        <CodeBlock lang={lang} raw={raw}>
          {child}
        </CodeBlock>
      );
    }
    return <pre className="code box-flat my-3 overflow-x-auto bg-sunk p-3">{children}</pre>;
  },
};

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[[rehypeHighlight, { detect: false, ignoreMissing: true }]]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
