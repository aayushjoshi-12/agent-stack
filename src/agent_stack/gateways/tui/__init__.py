"""The TUI gateway package: construct and run the Textual app."""

from __future__ import annotations

from agent_stack.app.application import Application
from agent_stack.gateways.tui.app import TuiApp

__all__ = ["TuiApp", "run_tui"]


def run_tui(application: Application, *, session_id: str | None = None) -> None:
    """Run the TUI against a wired application (blocking), optionally continuing a conversation.

    On the way out it prints how to come back to the conversation.
    """
    app = TuiApp(application, session_id=session_id, resume=session_id is not None)
    app.run()
    command = app.resume_command
    if command is not None:
        print(f"\nConversation {app.session_title or 'untitled'!r} saved.")
        print(f"Continue it with:\n  {command}")
