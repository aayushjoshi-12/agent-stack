from __future__ import annotations


def main() -> None:
    """Console-script entry point (``agent-stack``)."""
    from agent_stack.observability.cli import main as _cli_main

    raise SystemExit(_cli_main())
