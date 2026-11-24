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
from agent_stack.memory.ideas import IdeaInbox
from agent_stack.observability import history_cli
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
    run_parser = sub.add_parser("run", help="run one prompt non-interactively, then exit")
    run_parser.add_argument("prompt", help="the task, or '-' to read it from stdin")
    run_parser.add_argument(
        "--yes", action="store_true", help="approve every gated tool call (only inside a sandbox)"
    )
    ideas = sub.add_parser("ideas", help="the cross-project ideas inbox (~/.agent-stack/ideas)")
    ideas_sub = ideas.add_subparsers(dest="ideas_command", required=True)
    ideas_list = ideas_sub.add_parser("list", help="list ideas, optionally for one project")
    ideas_list.add_argument("--project", default=None)
    ideas_add = ideas_sub.add_parser("add", help="save an idea")
    ideas_add.add_argument("project")
    ideas_add.add_argument("title")
    ideas_add.add_argument("body", nargs="?", default="-", help="the idea, or '-' for stdin")
    ideas_show = ideas_sub.add_parser("show", help="print one idea")
    ideas_show.add_argument("project")
    ideas_show.add_argument("title")
    history_cli.add_parsers(sub)
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


def _run_once(prompt: str, yes: bool) -> int:
    import asyncio

    from agent_stack.app.bootstrap import build_application
    from agent_stack.gateways.cli import run_once

    text = sys.stdin.read() if prompt == "-" else prompt
    return asyncio.run(run_once(build_application(), text, approve_all=yes))


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


def _history(parsed: argparse.Namespace) -> int:
    import asyncio

    from agent_stack.app.bootstrap import build_application

    return asyncio.run(history_cli.run(build_application(), parsed))


def _ideas(parsed: argparse.Namespace) -> int:
    inbox = IdeaInbox(AppConfig.from_env().home)
    try:
        if parsed.ideas_command == "list":
            print("\n".join(inbox.list(parsed.project)) or "No ideas yet.")
        elif parsed.ideas_command == "add":
            body = sys.stdin.read() if parsed.body == "-" else parsed.body
            print(f"saved {inbox.add(parsed.project, parsed.title, body)}")
        else:
            print(inbox.read(parsed.project, parsed.title))
    except (ValueError, FileNotFoundError) as exc:
        print(f"ideas: {exc}", file=sys.stderr)
        return 1
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
    if parsed.command == "run":
        return _run_once(parsed.prompt, parsed.yes)
    if parsed.command in history_cli.COMMANDS:
        return _history(parsed)
    if parsed.command == "ideas":
        return _ideas(parsed)
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
