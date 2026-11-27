"""The Telegram gateway package: settings, the Bot API client and the adapter."""

from __future__ import annotations

import asyncio

from agent_stack.app.application import Application
from agent_stack.gateways.telegram.api import TelegramApi, TelegramError
from agent_stack.gateways.telegram.gateway import (
    TelegramConfigError,
    TelegramGateway,
    TelegramSettings,
    split_message,
)

__all__ = [
    "TelegramApi",
    "TelegramConfigError",
    "TelegramError",
    "TelegramGateway",
    "TelegramSettings",
    "run_telegram",
    "split_message",
]


def run_telegram(application: Application, settings: TelegramSettings) -> None:
    """Run the bot against a wired application (blocking; Ctrl+C to stop)."""

    async def main() -> None:
        api = TelegramApi(settings.token)
        try:
            await TelegramGateway(application, api, settings).run()
        finally:
            await application.drain_background()  # MCP servers
            await api.aclose()

    asyncio.run(main())
