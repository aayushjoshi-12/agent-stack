"""The TUI's colour themes.

Colours are named by role (Textual's theme variables), never referenced as literals in
the stylesheet, so any registered theme (including the terminal-native ``ansi-*`` ones)
restyles the whole app. These two are true-colour and tuned for contrast, and depth comes
from three background layers (background < surface < panel) instead of borders.
"""

from __future__ import annotations

from textual.theme import Theme

__all__ = ["DARK", "DEFAULT_THEME", "LIGHT", "THEMES"]

DEFAULT_THEME = "agent-stack-dark"

DARK = Theme(
    name="agent-stack-dark",
    primary="#7c9cff",
    secondary="#b08cff",
    accent="#4fd1c5",
    success="#7bd88f",
    warning="#f2c26b",
    error="#ff7a85",
    foreground="#d8dee9",
    background="#0f1218",
    surface="#151a22",
    panel="#1c232d",
    dark=True,
    variables={
        "footer-key-foreground": "#7c9cff",
        "footer-description-foreground": "#8892a0",
        "footer-background": "#151a22",
        "scrollbar": "#2a3340",
        "scrollbar-hover": "#3a4656",
        "scrollbar-active": "#7c9cff",
        "scrollbar-background": "#0f1218",
    },
)

LIGHT = Theme(
    name="agent-stack-light",
    primary="#3b5bdb",
    secondary="#7048e8",
    accent="#0b8f86",
    success="#2f9e44",
    warning="#b7791f",
    error="#d6336c",
    foreground="#1f2430",
    background="#fbfbfd",
    surface="#f1f3f7",
    panel="#e6e9f0",
    dark=False,
    variables={
        "footer-key-foreground": "#3b5bdb",
        "footer-description-foreground": "#5b6475",
        "footer-background": "#f1f3f7",
        "scrollbar": "#cdd3de",
        "scrollbar-hover": "#b4bccb",
        "scrollbar-active": "#3b5bdb",
        "scrollbar-background": "#fbfbfd",
    },
)

THEMES: tuple[Theme, ...] = (DARK, LIGHT)
