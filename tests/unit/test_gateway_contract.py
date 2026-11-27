"""P19 gateway shape test: gateway modules import only the contract, never internals.

Parses each gateway source file's import statements and asserts every
``agent_stack.*`` import is one of the allowed boundary modules. This is what keeps the
TUI/REPL from reaching around the Application into core/tools/stores.
"""

from __future__ import annotations

import ast
from pathlib import Path

from agent_stack.gateways.base import Gateway, MessageEnvelope

#: Every gateway source file that must respect the boundary (entry points excluded).
_GATEWAY_FILES = (
    "src/agent_stack/gateways/base.py",
    "src/agent_stack/gateways/cli.py",
    "src/agent_stack/gateways/tui/__init__.py",
    "src/agent_stack/gateways/tui/app.py",
    "src/agent_stack/gateways/tui/render.py",
    "src/agent_stack/gateways/telegram/__init__.py",
    "src/agent_stack/gateways/telegram/api.py",
    "src/agent_stack/gateways/telegram/gateway.py",
    "src/agent_stack/gateways/tui/theme.py",
    "src/agent_stack/gateways/tui/chrome.py",
    "src/agent_stack/gateways/tui/commands.py",
    "src/agent_stack/gateways/tui/rows.py",
    "src/agent_stack/gateways/tui/runs.py",
    "src/agent_stack/gateways/tui/screens.py",
    "src/agent_stack/gateways/tui/sessions.py",
    "src/agent_stack/gateways/tui/status.py",
    "src/agent_stack/gateways/tui/tabs.py",
    "src/agent_stack/gateways/tui/timeline.py",
)

#: The only internal modules a gateway may import (plus its own gateway package, never another gateway's).
_ALLOWED = frozenset(
    {
        "agent_stack.app.application",
        "agent_stack.core.events",
        "agent_stack.gateways.base",
    }
)


def _imported_agent_stack_modules(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module.startswith("agent_stack"):
                found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("agent_stack"):
                    found.add(alias.name)
    return found


def test_gateway_modules_import_only_the_contract() -> None:
    for filename in _GATEWAY_FILES:
        path = Path(filename)
        assert path.exists(), f"missing gateway module {filename}"
        for target in _imported_agent_stack_modules(path.read_text()):
            own = (
                "agent_stack.gateways.telegram"
                if "telegram" in filename
                else "agent_stack.gateways.tui"
            )
            allowed = target in _ALLOWED or target.startswith(own)  # a gateway uses only itself
            assert allowed, f"{filename} imports forbidden module {target}"


def test_gateway_contract_is_importable() -> None:
    envelope = MessageEnvelope(principal="u1", session="s1", text="hi")
    assert envelope.principal == "u1"
    assert hasattr(Gateway, "handle_message")
