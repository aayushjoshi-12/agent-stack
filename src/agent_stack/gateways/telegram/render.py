"""Turning agent text and tool calls into Telegram HTML.

Telegram's HTML subset is far less fragile than MarkdownV2: only ``& < >`` need escaping. We
convert the three things models actually write (fenced code, inline code, bold) and escape
everything else, so arbitrary model or tool output can never produce an invalid message.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Mapping
from typing import Any

from agent_stack.gateways.base import HistoryItem

__all__ = ["escape_fit", "format_tool_detail", "to_html", "tool_label"]

_FENCE = re.compile(r"```[ \t]*([\w+-]*)\n?(.*?)```", re.DOTALL)
_INLINE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")


def _inline(text: str) -> str:
    """Escape ``text``, then turn `code` and **bold** into tags (code is never bolded)."""
    out: list[str] = []
    last = 0
    for match in _INLINE.finditer(text):
        out.append(_BOLD.sub(r"<b>\1</b>", html.escape(text[last : match.start()], quote=False)))
        out.append(f"<code>{html.escape(match.group(1), quote=False)}</code>")
        last = match.end()
    out.append(_BOLD.sub(r"<b>\1</b>", html.escape(text[last:], quote=False)))
    return "".join(out)


def to_html(text: str) -> str:
    """Model markdown -> Telegram HTML. Unbalanced markers stay literal (they are escaped)."""
    out: list[str] = []
    last = 0
    for match in _FENCE.finditer(text):
        out.append(_inline(text[last : match.start()]))
        body = html.escape(match.group(2).rstrip("\n"), quote=False)
        lang = match.group(1)
        cls = f' class="language-{html.escape(lang)}"' if lang else ""
        out.append(f"<pre><code{cls}>{body}</code></pre>")
        last = match.end()
    out.append(_inline(text[last:]))
    return "".join(out)


def _short(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_label(name: str, args: Mapping[str, Any] | None, limit: int = 48) -> str:
    """A one-line label: the tool and its command, else its first short string argument."""
    detail = ""
    if args:
        command = args.get("command")
        short = [v for v in args.values() if isinstance(v, str) and len(v) < 80]
        detail = str(command) if command else (short[0] if short else "")
    return _short(f"{name} {detail}".strip().replace("\n", " "), limit)


def escape_fit(text: str, limit: int) -> str:
    """HTML-escape ``text`` and cut it so the *escaped* result fits ``limit`` characters."""
    escaped = html.escape(text, quote=False)
    while len(escaped) > limit:
        text = text[: max(int(len(text) * 0.8), 1)] + "…"
        escaped = html.escape(text, quote=False)
    return escaped


def format_tool_detail(item: HistoryItem, *, limit: int = 3800) -> str:
    """HTML for one tool call: its name, full arguments and result, within one message."""
    parts = [f"<b>{html.escape(item.text)}</b>"]
    if item.args:
        args = json.dumps(dict(item.args), ensure_ascii=False, indent=2, default=str)
        parts.append(f"<b>arguments</b>\n<pre>{escape_fit(args, limit // 3)}</pre>")
    used = sum(len(p) for p in parts) + 60
    if item.result is None:
        parts.append("<i>no result recorded</i>")
    else:
        parts.append(f"<b>result</b>\n<pre>{escape_fit(item.result, max(limit - used, 300))}</pre>")
    return "\n".join(parts)
