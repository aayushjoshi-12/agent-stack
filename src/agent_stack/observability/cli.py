"""Minimal ``agent-stack audit`` CLI (P8b).

Only the one command gateways need today: ``audit tail`` follows the JSONL trace and
filters by run. No arguments fall through to the normal app startup, so
``python -m agent_stack`` still starts.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from agent_stack.app.config import AppConfig
from agent_stack.observability.audit import audit_log_path

__all__ = ["main", "tail"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-stack")
    sub = parser.add_subparsers(dest="command")
    audit = sub.add_parser("audit", help="inspect the event audit log")
    audit_sub = audit.add_subparsers(dest="audit_command")
    tail_parser = audit_sub.add_parser("tail", help="follow the audit log")
    tail_parser.add_argument("--run-id", default=None, help="show only this run's lines")
    tail_parser.add_argument(
        "--from-sequence", type=int, default=0, help="skip sequences below this"
    )
    return parser


def _matches(record: Mapping[str, Any], run_id: str | None, from_sequence: int) -> bool:
    if run_id is not None and record.get("run_id") != run_id:
        return False
    return int(record.get("sequence", 0)) >= from_sequence


def tail(path: Path, *, run_id: str | None = None, from_sequence: int = 0) -> None:
    """Follow the audit log, printing matching lines until interrupted."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    with path.open(encoding="utf-8") as handle, contextlib.suppress(KeyboardInterrupt):
        while True:
            line = handle.readline()
            if not line:
                time.sleep(0.2)
                continue
            if _matches(json.loads(line), run_id, from_sequence):
                print(line, end="")


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch the CLI; no arguments start the app (the pre-CLI behaviour)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        from agent_stack.app.bootstrap import main as ready

        ready()
        return 0

    parsed = _parser().parse_args(args)
    if parsed.command == "audit" and parsed.audit_command == "tail":
        path = audit_log_path(AppConfig.from_env().workspace)
        tail(path, run_id=parsed.run_id, from_sequence=parsed.from_sequence)
        return 0

    _parser().print_help()
    return 1
