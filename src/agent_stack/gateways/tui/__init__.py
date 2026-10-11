"""The TUI gateway package: construct and run the Textual app."""

from __future__ import annotations

from agent_stack.app.application import Application
from agent_stack.gateways.tui.app import TuiApp

__all__ = ["TuiApp", "run_tui"]


def run_tui(application: Application) -> None:
    """Run the TUI against a wired application (blocking)."""
    TuiApp(application).run()
