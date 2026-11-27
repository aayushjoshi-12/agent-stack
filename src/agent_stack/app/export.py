"""A conversation as a file (P41): Markdown to read, JSON to keep.

Both are built from the stored transcript rows after redaction: every string goes through the
same redactor as the event and audit logs, and values under secret-named keys are dropped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

from agent_stack.core.context import COMPACTION_HEADER, FILES_HEADER
from agent_stack.observability.redact import REDACTED, redact

__all__ = ["FORMATS", "export_filename", "free_path", "parse_export_args", "render", "scrub"]

FORMATS = ("md", "json")
_RESULT_CHARS = 4000  # a tool result longer than this is cut in the Markdown (JSON keeps all)
_SECRET_KEYS = ("key", "token", "secret", "password")
_SKILL_BLOCK_RE = re.compile(r"\n*<skill-invocation .*?</skill-invocation>", re.DOTALL)


def scrub(value: Any, known: Sequence[str]) -> Any:
    """``value`` with secrets redacted in every string and secret-named keys blanked."""
    if isinstance(value, str):
        return redact(value, known)
    if isinstance(value, Mapping):
        return {
            str(k): REDACTED
            if isinstance(v, str) and any(m in str(k).lower() for m in _SECRET_KEYS)
            else scrub(v, known)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v, known) for v in value]
    return value


def parse_export_args(argument: str) -> tuple[str, str | None]:
    """``/export [md|json] [path]`` -> ``(fmt, path)``; a bare ``x.json`` path implies json."""
    head, _, rest = argument.strip().partition(" ")
    if head.lower() in FORMATS:
        return head.lower(), rest.strip() or None
    path = argument.strip() or None
    return ("json" if path and path.lower().endswith(".json") else "md"), path


def render(fmt: str, meta: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    """The export text: ``meta`` is session metadata, ``rows`` the (redacted) stored messages."""
    if fmt == "json":
        return json.dumps({"session": meta, "messages": list(rows)}, indent=2, ensure_ascii=False)
    if fmt == "md":
        return _markdown(meta, rows)
    raise ValueError(f"export format must be one of {', '.join(FORMATS)}, not {fmt!r}")


def export_filename(title: str | None, fmt: str, day: date) -> str:
    """``<slug-of-title>-<YYYY-MM-DD>.<fmt>``."""
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:60].strip("-")
    return f"{slug or 'conversation'}-{day.isoformat()}.{fmt}"


def free_path(path: Path) -> Path:
    """``path``, or ``name-2.ext``, ``name-3.ext``... when it is taken: never overwrite."""
    candidate, n = path, 1
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
    return candidate


def _markdown(meta: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    results = {
        r.get("tool_call_id"): str(r.get("content", "")) for r in rows if r["kind"] == "tool"
    }
    started = str(meta.get("created_at") or "")[:10]
    out = [
        f"# {meta.get('title') or 'Untitled conversation'}",
        "",
        f"Exported {str(meta.get('exported_at', ''))[:10]}"
        + (f" · started {started}" if started else "")
        + f" · conversation `{meta.get('id')}`",
    ]
    for row in rows:
        kind, text = row["kind"], str(row.get("content", ""))
        if kind == "human":
            text = _SKILL_BLOCK_RE.sub("", text).strip()
            if text:
                out += ["", "---", "", f"## You{_when(row)}", "", text]
        elif kind == "system" and text.startswith(COMPACTION_HEADER):
            summary = text.split(FILES_HEADER)[0][len(COMPACTION_HEADER) :].strip()
            quoted = "\n".join(f"> {line}" if line else ">" for line in summary.splitlines())
            out += ["", "---", "", "> **Compacted:** the conversation above was summarised as:"]
            out += [">", quoted]
        elif kind == "ai":
            if text.strip() or row.get("tool_calls"):
                out += ["", f"## Agent{_when(row)}"]
            if text.strip():
                out += ["", text.strip()]
            for call in row.get("tool_calls") or ():
                out += _tool_call(call, results.get(call.get("id")))
    return "\n".join(out) + "\n"


def _when(row: Mapping[str, Any]) -> str:
    ts = str(row.get("ts") or "")
    return f" · {ts[:16].replace('T', ' ')}" if ts else ""


def _tool_call(call: Mapping[str, Any], result: str | None) -> list[str]:
    args = json.dumps(call.get("args") or {}, indent=2, ensure_ascii=False)
    lines = ["", f"**Tool `{call.get('name')}`**", "", _fence(args, "json")]
    if result is None:
        return [*lines, "", "_(no result)_"]
    shown = result
    if len(result) > _RESULT_CHARS:
        shown = result[:_RESULT_CHARS] + f"\n... [{len(result) - _RESULT_CHARS} more characters]"
    return [
        *lines,
        "",
        f"<details><summary>Result ({len(result)} characters)</summary>",
        "",
        _fence(shown.rstrip("\n")),
        "",
        "</details>",
    ]


def _fence(text: str, lang: str = "") -> str:
    """A code fence longer than any backtick run inside ``text``."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"
