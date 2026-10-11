"""Append-only audit log of runtime events (P8b).

The event bus writes one JSONL line per published event here — the bus is the single
choke point, so nothing can be emitted unlogged. Lines are capped and redacted so a
debug trace never leaks secrets and never balloons with tool output.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent_stack.core.events import AgentEvent

__all__ = ["AuditLogger", "audit_log_path", "read_audit"]

# Honest, tiny rules: a key name matching any marker is dropped whole; long values are
# truncated to a byte budget (tool output can be megabytes).
_SECRET_MARKERS = ("key", "token", "secret", "password")
_REDACTED = "[redacted]"
_MAX_FIELD_BYTES = 2000


def audit_log_path(workspace: Path) -> Path:
    """The audit log for a workspace (sessions live beside it under ``.agent/``)."""
    return workspace / ".agent" / "logs" / "audit.jsonl"


def _is_secret(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SECRET_MARKERS)


def _cap(value: str) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= _MAX_FIELD_BYTES:
        return value
    removed = len(raw) - _MAX_FIELD_BYTES
    kept = raw[:_MAX_FIELD_BYTES].decode("utf-8", "ignore")
    return f"{kept}...[truncated {removed} bytes]"


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _cap(value)
    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED if _is_secret(str(key)) else _sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    return value


def audit_record(event: AgentEvent) -> dict[str, Any]:
    """The one-line schema: timestamp, identity, ordering, type, capped/redacted data."""
    return {
        "ts": event.timestamp.isoformat(),
        "run_id": event.run_id,
        "session_id": event.session_id,
        "sequence": event.sequence,
        "type": event.type.value,
        "data": _sanitize(dict(event.data)),
    }


class AuditLogger:
    """Append-only JSONL sink for the event bus.

    Opening creates the parent directories. Writes are synchronous and never await, so
    a single ``write`` call cannot interleave with another on the same event loop.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("a", encoding="utf-8")

    def write(self, event: AgentEvent) -> None:
        """Append one audit line for ``event`` and flush so a live tail sees it."""
        line = json.dumps(audit_record(event), ensure_ascii=False, default=str)
        self._file.write(line + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def read_audit(path: Path) -> list[dict[str, Any]]:
    """Read the audit log in file order (empty when unset). Used for replay and tail."""
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]
