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
    primary="#2f49c4",
    secondary="#6a3fe0",
    accent="#0a766e",
    success="#1d7a35",
    warning="#9a6200",
    error="#c2255c",
    foreground="#1b2030",
    background="#fbfbfd",
    surface="#eaedf3",
    panel="#d9dee9",
    dark=False,
    variables={
        "foreground-muted": "#4a5266",
        "footer-key-foreground": "#2f49c4",
        "footer-description-foreground": "#3f475a",
        "footer-background": "#eaedf3",
        "scrollbar": "#c3cad8",
        "scrollbar-hover": "#a9b2c4",
        "scrollbar-active": "#2f49c4",
        "scrollbar-background": "#fbfbfd",
    },
)

THEMES: tuple[Theme, ...] = (DARK, LIGHT)
