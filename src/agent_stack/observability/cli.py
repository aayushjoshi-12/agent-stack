"""The ``agent-stack`` command line (P8b/P23).

With no arguments it launches the TUI (the single-command entry point). Subcommands:
``tui``, ``chat`` (the line REPL), and ``audit tail``.
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
    sub.add_parser("tui", help="run the Textual terminal UI gateway")
    sub.add_parser("chat", help="run the line REPL gateway")
    sub.add_parser("telegram", help="run the Telegram bot gateway (needs TELEGRAM_BOT_TOKEN)")
    sub.add_parser("web", help="run the web API gateway (needs AGENT_STACK_WEB_TOKEN)")
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


def _run_tui() -> int:
    from agent_stack.app.bootstrap import build_application
    from agent_stack.gateways.tui import run_tui

    run_tui(build_application())
    return 0


def _run_chat() -> int:
    import asyncio

    from agent_stack.app.bootstrap import build_application
    from agent_stack.gateways.cli import run_cli

    asyncio.run(run_cli(build_application()))
    return 0


def _run_telegram() -> int:
    import os
    from dataclasses import replace

    from agent_stack.app.bootstrap import build_application
    from agent_stack.gateways.telegram import TelegramConfigError, TelegramSettings, run_telegram

    try:
        settings = TelegramSettings.from_env(os.environ)
    except TelegramConfigError as exc:
        print(f"telegram: {exc}", file=sys.stderr)
        return 2
    application = build_application()
    if settings.state_path is None:  # remember each chat's open conversation across restarts
        settings = replace(
            settings, state_path=application.config.workspace / ".agent" / "telegram_state.json"
        )
    run_telegram(application, settings)
    return 0


def _run_web() -> int:
    import os

    from agent_stack.app.bootstrap import build_application
    from agent_stack.gateways.web import WebConfigError, WebSettings, run_web

    try:
        settings = WebSettings.from_env(os.environ)
    except WebConfigError as exc:
        print(f"web: {exc}", file=sys.stderr)
        return 2
    run_web(build_application(), settings)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch the CLI; with no arguments, launch the TUI."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _run_tui()

    parsed = _parser().parse_args(args)
    if parsed.command == "audit" and parsed.audit_command == "tail":
        path = audit_log_path(AppConfig.from_env().workspace)
        tail(path, run_id=parsed.run_id, from_sequence=parsed.from_sequence)
        return 0
    if parsed.command == "tui":
        return _run_tui()
    if parsed.command == "chat":
        return _run_chat()
    if parsed.command == "telegram":
        return _run_telegram()
    if parsed.command == "web":
        return _run_web()

    _parser().print_help()
    return 1
