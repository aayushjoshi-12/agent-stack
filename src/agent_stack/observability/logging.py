"""Structured JSON logs keyed by run (P27).

One JSON object per line with ``ts``, ``level``, ``logger``, ``message`` and, when the record is
about a run, ``run_id`` / ``session_id`` / ``parent_run_id`` / ``trace_id``. ``trace_id`` is the
root run of a delegation chain, so a subagent's lines group under the run that started it.
Messages and string fields are passed through :func:`redact`, so a secret never reaches the file.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_stack.core.events import AgentEvent
from agent_stack.core.run_context import current_run_context
from agent_stack.observability.redact import env_secrets, redact

__all__ = ["EventLogger", "JsonLogFormatter", "configure_logging"]

_RUN_FIELDS = ("run_id", "session_id", "parent_run_id", "trace_id")
_LOGGER = "agent_stack"


class JsonLogFormatter(logging.Formatter):
    """Format a record as one redacted JSON line; run identity comes from ``extra`` or context."""

    def __init__(self, known_secrets: list[str] | None = None) -> None:
        super().__init__()
        self._known = known_secrets if known_secrets is not None else env_secrets()

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact(record.getMessage(), self._known),
        }
        context = current_run_context()
        for name in _RUN_FIELDS:
            value = getattr(record, name, None)
            if value is None and context is not None:
                value = {"run_id": context.run_id, "session_id": context.session_id}.get(name)
            if value is not None:
                entry[name] = value
        if (data := getattr(record, "data", None)) is not None:
            entry["data"] = redact(json.dumps(data, default=str, ensure_ascii=False), self._known)
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info), self._known)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(path: Path, level: int = logging.INFO) -> logging.Logger:
    """Send ``agent_stack`` logs to ``path`` as JSON lines. Idempotent per file."""
    logger = logging.getLogger(_LOGGER)
    logger.setLevel(level)
    logger.propagate = False  # never leak into a TUI's terminal through the root logger
    resolved = path.resolve()
    for handler in logger.handlers:
        if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename) == resolved:
            return logger
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(JsonLogFormatter())
    logger.addHandler(handler)
    return logger


class EventLogger:
    """A bus sink that logs one line per event and stamps each with its ``trace_id``."""

    def __init__(self) -> None:
        self._log = logging.getLogger(f"{_LOGGER}.events")
        self._root: dict[str, str] = {}

    def write(self, event: AgentEvent) -> None:
        parent = event.parent_run_id
        root = event.run_id if parent is None else self._root.get(parent, parent)
        self._root.setdefault(event.run_id, root)
        level = logging.WARNING if event.type.value == "run.failed" else logging.INFO
        self._log.log(
            level,
            event.type.value,
            extra={
                "run_id": event.run_id,
                "session_id": event.session_id,
                "parent_run_id": parent,
                "trace_id": root,
                "data": dict(event.data),
            },
        )
