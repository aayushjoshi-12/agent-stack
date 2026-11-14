"""A minimal Telegram Bot API client over ``httpx`` (no extra dependency).

Only the handful of methods the gateway needs. Errors never carry the request URL: it contains
the bot token, and exception text ends up in logs.
"""

from __future__ import annotations

from typing import Any

import httpx

__all__ = ["TelegramApi", "TelegramError"]

_BASE = "https://api.telegram.org"


class TelegramError(Exception):
    """A failed Bot API call. ``code`` is Telegram's error code (or 0 for a network failure)."""

    def __init__(self, message: str, code: int = 0) -> None:
        super().__init__(message)
        self.code = code


class TelegramApi:
    """Calls ``POST /bot<token>/<method>`` with JSON and unwraps Telegram's envelope."""

    def __init__(self, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self._client = client or httpx.AsyncClient(base_url=_BASE)

    async def call(self, method: str, *, http_timeout: float = 15.0, **params: Any) -> Any:
        try:
            response = await self._client.post(
                f"/bot{self._token}/{method}",
                json={k: v for k, v in params.items() if v is not None},
                timeout=http_timeout,
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(f"{type(exc).__name__} calling {method}") from None
        if not payload.get("ok"):
            raise TelegramError(
                str(payload.get("description", "request failed")), int(payload.get("error_code", 0))
            )
        return payload["result"]

    async def get_updates(self, offset: int, poll_seconds: int = 30) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = await self.call(
            "getUpdates",
            http_timeout=poll_seconds + 10,  # the HTTP timeout must outlast the long poll
            offset=offset,
            timeout=poll_seconds,
            allowed_updates=["message", "callback_query"],
        )
        return result

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> int:
        """Send text (plain, or ``parse_mode="HTML"``); returns the new message's id."""
        result = await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return int(result["message_id"])

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        await self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup if reply_markup is not None else {"inline_keyboard": []},
        )

    async def set_my_commands(self, commands: list[tuple[str, str]]) -> None:
        """Publish the slash-command menu shown next to the message box."""
        await self.call(
            "setMyCommands",
            commands=[{"command": name, "description": text} for name, text in commands],
        )

    async def answer_callback_query(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text or None)

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self.call("sendChatAction", chat_id=chat_id, action=action)

    async def aclose(self) -> None:
        await self._client.aclose()
