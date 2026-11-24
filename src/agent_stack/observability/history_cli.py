"""Command-line access to conversation history: turns, tree, fork, rewind, switch, references."""

from __future__ import annotations

import argparse
import html
from pathlib import Path
from typing import TYPE_CHECKING

from agent_stack.app.timeline import render_ascii, render_mermaid
from agent_stack.core.errors import PersistenceError

if TYPE_CHECKING:
    from agent_stack.app.application import Application

__all__ = ["COMMANDS", "add_parsers", "run"]

COMMANDS = frozenset({"sessions", "turns", "tree", "fork", "rewind", "switch", "ref"})

_PAGE = """<!doctype html><meta charset="utf-8"><title>Conversation branches</title>
<style>body{{font:15px system-ui;margin:2rem;background:#fff;color:#111}}
@media(prefers-color-scheme:dark){{body{{background:#161616;color:#eee}}}}</style>
<h1>Conversation branches</h1><pre class="mermaid">{graph}</pre>
<h2>As text</h2><pre>{text}</pre>
<script type="module">import m from "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs";
m.initialize({{startOnLoad:true,securityLevel:"strict"}});</script>"""


def add_parsers(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    sub.add_parser("sessions", help="list every conversation, branches marked")
    turns = sub.add_parser("turns", help="a conversation's turns and the points around them")
    turns.add_argument("session")
    tree = sub.add_parser("tree", help="draw the branches a conversation belongs to")
    tree.add_argument("session")
    tree.add_argument("--mermaid", action="store_true", help="print Mermaid instead of text")
    tree.add_argument("--no-files", action="store_true", help="leave the changed files out")
    tree.add_argument("--html", metavar="FILE", help="write a page that draws the graph")
    fork = sub.add_parser("fork", help="fork at a point; the original and the files stay as is")
    fork.add_argument("session")
    fork.add_argument("point", help="a turn number (3 = after turn 3, 3- = before it) or event id")
    rewind = sub.add_parser("rewind", help="go back to a point: fork there and restore the files")
    rewind.add_argument("session")
    rewind.add_argument("point")
    rewind.add_argument("--keep-files", action="store_true", help="do not touch the folder")
    rewind.add_argument("--force", action="store_true", help="overwrite files edited by hand")
    switch = sub.add_parser("switch", help="put the folder in another conversation's file state")
    switch.add_argument("from_session")
    switch.add_argument("to_session")
    switch.add_argument("--force", action="store_true")
    ref = sub.add_parser("ref", help="share a point in a conversation")
    ref_sub = ref.add_subparsers(dest="ref_command", required=True)
    make = ref_sub.add_parser("make", help="print a reference to a point")
    make.add_argument("session")
    make.add_argument("point")
    show = ref_sub.add_parser("show", help="print what happened up to a referenced point")
    show.add_argument("reference")
    opened = ref_sub.add_parser("open", help="start a new conversation from a referenced point")
    opened.add_argument("reference")


async def run(app: Application, args: argparse.Namespace) -> int:
    """Run one history command; returns the exit code. Errors print one line, never a traceback."""
    try:
        return await _run(app, args)
    except PersistenceError as exc:
        print(f"error: {exc}")
        return 1


async def _run(app: Application, args: argparse.Namespace) -> int:
    timeline = app.timeline
    command = args.command
    if command == "sessions":
        store = app.session_store
        if not hasattr(store, "list_all"):
            raise PersistenceError("this session store cannot list every conversation")
        for session in await store.list_all(200):
            parent = f"  (branch of {session.branch_of})" if session.branch_of else ""
            print(f"{session.id}  {session.title or 'untitled'}{parent}")
        return 0
    if command == "turns":
        for turn in await timeline.turns(args.session):
            end = "done" if turn.completed_event_id else "running"
            print(
                f"{turn.index:>3}  {turn.timestamp:%Y-%m-%d %H:%M:%S}  {end:<7} "
                f"{turn.files_changed} file(s)  {turn.user_text[:70]!r}"
            )
        return 0
    if command == "tree":
        root = await timeline.tree(args.session)
        if args.mermaid:
            print(render_mermaid(root, current=args.session))
        else:
            print(render_ascii(root, current=args.session, files=not args.no_files))
        if args.html:
            graph = render_mermaid(root, current=args.session)
            text = render_ascii(root, current=args.session)
            Path(args.html).write_text(
                _PAGE.format(graph=html.escape(graph), text=html.escape(text)), encoding="utf-8"
            )
            print(f"wrote {args.html}")
        return 0
    if command == "fork":
        print(await timeline.fork(args.session, args.point))
        return 0
    if command == "rewind":
        branch, result = await timeline.rewind(
            args.session, args.point, restore_files=not args.keep_files, force=args.force
        )
        print(f"new conversation {branch} (the original {args.session} is unchanged)")
        _print_files(result)
        return 1 if result.conflicts else 0
    if command == "switch":
        result = await timeline.switch_files(args.from_session, args.to_session, force=args.force)
        _print_files(result)
        return 1 if result.conflicts else 0
    if command == "ref":
        if args.ref_command == "make":
            print(await timeline.reference(args.session, args.point))
        elif args.ref_command == "show":
            print(await timeline.describe(args.reference))
        else:
            opened = await timeline.open_reference(args.reference)
            print(
                f"new conversation {opened}; put its files in place with: "
                f"agent-stack switch <your current conversation> {opened}"
            )
        return 0
    return 1


def _print_files(result: object) -> None:
    moved = getattr(result, "moved", [])
    conflicts = getattr(result, "conflicts", [])
    for move in moved:
        print(f"  {move.action} {move.path}")
    if not moved and not conflicts:
        print("  no files needed to change")
    for path in conflicts:
        print(f"  CONFLICT {path}: changed since; left alone (use --force to overwrite)")
