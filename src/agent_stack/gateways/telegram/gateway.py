"""The Telegram gateway: an adapter, nothing more.

Update -> :class:`MessageEnvelope` -> ``Application.send_message``; events come back and are
rendered as a debounced status message, replies and inline-keyboard approvals. Telegram's quirks
(long polling, the 4096-character limit, edit rate limits, duplicate updates, callback data
limits) stay in this package. Like every gateway it touches only ``Application``,
``gateways.base`` and ``core.events``.

Security: the bot drives tools on the machine it runs on, so **only allow-listed Telegram user
ids are served**; everyone else is ignored without a reply.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_stack.app.application import Application
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.gateways.base import MessageEnvelope, SessionSummary
from agent_stack.gateways.telegram.api import TelegramApi, TelegramError

__all__ = ["TelegramConfigError", "TelegramGateway", "TelegramSettings", "split_message"]

#: Telegram rejects messages longer than this many characters.
MESSAGE_LIMIT = 4096
_STATUS_LINES = 6
_SEEN_LIMIT = 2000
_HELP = (
    "Send a message to talk to the agent.\n"
    "/new  start a fresh conversation\n"
    "/sessions  list conversations and switch\n"
    "/cancel  stop the current run"
)


class TelegramConfigError(ValueError):
    """The bot is not configured safely (no token, or nobody allowed)."""


@dataclass(frozen=True)
class TelegramSettings:
    """Where the bot is, who may use it, and which identity its chats act as.

    ``principal`` is optional: set it to the TUI's identity (``tui:local-user``) and the bot's
    conversations *are* your TUI conversations; unset, each Telegram user gets their own.
    """

    token: str = field(repr=False)
    allowed_users: frozenset[int]
    principal: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> TelegramSettings:
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise TelegramConfigError("TELEGRAM_BOT_TOKEN is not set")
        try:
            users = frozenset(
                int(part)
                for part in env.get("AGENT_STACK_TELEGRAM_USERS", "").split(",")
                if part.strip()
            )
        except ValueError:
            raise TelegramConfigError("AGENT_STACK_TELEGRAM_USERS must be numeric ids") from None
        if not users:
            raise TelegramConfigError(
                "AGENT_STACK_TELEGRAM_USERS is empty: the bot runs tools on this machine, "
                "so it needs an explicit allow-list of Telegram user ids"
            )
        return cls(token, users, env.get("AGENT_STACK_TELEGRAM_PRINCIPAL") or None)


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split ``text`` into chunks of at most ``limit`` characters, preferring line breaks."""
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n") if cut < len(text) and text[cut] == "\n" else text[cut:]
    if text.strip() or not chunks:
        chunks.append(text)
    return chunks


@dataclass
class _Chat:
    """Per-chat state: the open conversation, the live run and its status message."""

    chat_id: int
    principal: str
    session: str | None = None  # None: a draft, stored on the first message
    title: str | None = None
    run_id: str | None = None
    task: asyncio.Task[None] | None = None
    status_id: int | None = None
    status_lines: list[str] = field(default_factory=list)
    last_edit: float = 0.0
    listing: list[SessionSummary] = field(default_factory=list)
    approvals: dict[str, tuple[str, str, int, str]] = field(default_factory=dict)

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()


def _summary(event: AgentEvent) -> str:
    """One status line for a tool call: its command, else its first short argument."""
    tool = str(event.data.get("tool", "tool"))
    args = event.data.get("args")
    detail = ""
    if isinstance(args, Mapping):
        command = args.get("command")
        short = [v for v in args.values() if isinstance(v, str) and len(v) < 60]
        detail = str(command) if command else (short[0] if short else "")
    return f"{tool} {detail}".strip()[:120]


class TelegramGateway:
    """Long-polls Telegram and drives the Application on behalf of allow-listed users."""

    def __init__(
        self,
        application: Application,
        api: TelegramApi,
        settings: TelegramSettings,
        *,
        edit_interval: float = 1.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._app = application
        self._api = api
        self._settings = settings
        self._edit_interval = edit_interval
        self._clock = clock
        self._sleep = sleep
        self._chats: dict[int, _Chat] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._stopped = False

    # ── polling loop ─────────────────────────────────────────────────────────
    async def run(self) -> None:
        """Poll until :meth:`stop`; transient network errors back off instead of ending it."""
        offset, delay = 0, 1.0
        while not self._stopped:
            try:
                updates = await self._api.get_updates(offset)
            except TelegramError as exc:
                if exc.code == 401:  # a bad token will never recover
                    raise
                await self._sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            delay = 1.0
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                await self.handle_update(update)

    def stop(self) -> None:
        self._stopped = True

    # ── one update ───────────────────────────────────────────────────────────
    async def handle_update(self, update: Mapping[str, Any]) -> None:
        """Dispatch a single update. Duplicates and strangers are dropped silently."""
        if not self._first_time(f"update:{update.get('update_id')}"):
            return
        callback = update.get("callback_query")
        if callback is not None:
            await self._on_callback(callback)
            return
        message = update.get("message")
        if not message or "text" not in message:
            return
        sender = int(message.get("from", {}).get("id", 0))
        if sender not in self._settings.allowed_users:
            return
        chat_id = int(message["chat"]["id"])
        key = f"message:{chat_id}:{message.get('message_id')}"
        if not self._first_time(key):
            return
        await self._on_text(self._chat(chat_id, sender), str(message["text"]).strip(), key)

    def _first_time(self, key: str) -> bool:
        """Idempotency: Telegram redelivers updates after a reconnect; act on each only once."""
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)
        return True

    def _chat(self, chat_id: int, user_id: int) -> _Chat:
        chat = self._chats.get(chat_id)
        if chat is None:
            principal = self._settings.principal or f"telegram:{user_id}"
            chat = self._chats[chat_id] = _Chat(chat_id=chat_id, principal=principal)
        return chat

    # ── text and commands ────────────────────────────────────────────────────
    async def _on_text(self, chat: _Chat, text: str, key: str) -> None:
        if not text:
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()  # "/new@mybot" in group chats
        if command in ("/start", "/help"):
            await self._say(chat, _HELP)
        elif command == "/new":
            await self._cmd_new(chat)
        elif command == "/sessions":
            await self._cmd_sessions(chat)
        elif command == "/cancel":
            await self._cmd_cancel(chat)
        elif chat.busy:
            await self._say(chat, "A run is in progress. Send /cancel to stop it first.")
        else:
            chat.task = asyncio.create_task(self._run_message(chat, text, key))

    async def _cmd_new(self, chat: _Chat) -> None:
        if chat.busy:
            await self._say(chat, "A run is in progress. Send /cancel to stop it first.")
            return
        chat.session, chat.title = None, None  # a draft, stored on the first message
        await self._say(chat, "New conversation. What would you like to do?")

    async def _cmd_cancel(self, chat: _Chat) -> None:
        if not chat.busy or chat.run_id is None:
            await self._say(chat, "Nothing is running.")
            return
        await self._app.cancel_run(chat.run_id)

    async def _cmd_sessions(self, chat: _Chat) -> None:
        if chat.busy:
            await self._say(chat, "A run is in progress. Send /cancel to stop it first.")
            return
        chat.listing = await self._app.list_sessions(chat.principal, limit=8)
        if not chat.listing:
            await self._say(chat, "No conversations yet.")
            return
        rows = [
            [
                {
                    "text": ("● " if s.id == chat.session else "") + s.title[:40],
                    "callback_data": f"sw:{i}",
                }
            ]
            for i, s in enumerate(chat.listing)
        ]
        await self._say(chat, "Conversations (tap to switch):", {"inline_keyboard": rows})

    # ── callbacks: switching and approvals ───────────────────────────────────
    async def _on_callback(self, callback: Mapping[str, Any]) -> None:
        sender = int(callback.get("from", {}).get("id", 0))
        message = callback.get("message") or {}
        if sender not in self._settings.allowed_users or "chat" not in message:
            return
        chat = self._chat(int(message["chat"]["id"]), sender)
        data = str(callback.get("data", ""))
        callback_id = str(callback.get("id", ""))
        if data.startswith("ap:"):
            await self._on_approval(chat, callback_id, data)
        elif data.startswith("sw:"):
            await self._on_switch(chat, callback_id, data)
        else:
            await self._api.answer_callback_query(callback_id)

    async def _on_approval(self, chat: _Chat, callback_id: str, data: str) -> None:
        _, approval_id, verdict = data.split(":", 2)
        pending = chat.approvals.get(approval_id)
        if pending is None:
            await self._api.answer_callback_query(callback_id, "This request is no longer pending.")
            return
        run_id, session_id, message_id, label = pending
        approved = verdict == "y"
        try:
            self._app.decide_approval(
                approval_id,
                run_id=run_id,
                session_id=session_id,
                principal=chat.principal,
                approve=approved,
            )
        except Exception as exc:  # e.g. the run was cancelled while the question was open
            await self._api.answer_callback_query(callback_id, str(exc)[:150])
            return
        chat.approvals.pop(approval_id, None)
        await self._api.answer_callback_query(callback_id, "Approved" if approved else "Rejected")
        await self._edit(
            chat.chat_id, message_id, f"{'✓ Approved' if approved else '✗ Rejected'}: {label}"
        )

    async def _on_switch(self, chat: _Chat, callback_id: str, data: str) -> None:
        if chat.busy:
            await self._api.answer_callback_query(callback_id, "A run is in progress.")
            return
        try:
            target = chat.listing[int(data.split(":", 1)[1])]
        except (ValueError, IndexError):
            await self._api.answer_callback_query(
                callback_id, "That list is out of date. Send /sessions."
            )
            return
        chat.session, chat.title = target.id, target.title
        await self._api.answer_callback_query(callback_id, "Switched")
        history = await self._app.session_history(target.id)
        recap = [
            f"{'You' if h.kind == 'user' else 'Agent'}: {h.text[:300]}"
            for h in history
            if h.kind != "tool"
        ]
        await self._say(chat, f"Switched to “{target.title}”.\n\n" + "\n\n".join(recap[-2:]))

    # ── one run ──────────────────────────────────────────────────────────────
    async def _run_message(self, chat: _Chat, text: str, key: str) -> None:
        try:
            if chat.session is None:
                chat.session = await self._app.new_session(chat.principal)
            if chat.title is None:
                chat.title = text[:80]
                await self._app.set_session_title(chat.session, chat.title)
            with_typing = asyncio.create_task(self._api.send_chat_action(chat.chat_id))
            chat.status_lines = []
            chat.status_id = await self._api.send_message(chat.chat_id, "⏳ working…")
            chat.last_edit = self._clock()
            await with_typing
            handle = await self._app.send_message(
                MessageEnvelope(
                    principal=chat.principal,
                    session=chat.session,
                    text=text,
                    metadata={"idempotency_key": key},
                )
            )
            chat.run_id = handle.run_id
            finished = False
            async for event in handle.events():
                finished = await self._on_event(chat, event) or finished
            if not finished:  # a hard cancel ends the stream with no terminal event
                await self._finish_status(chat, "■ cancelled")
        except Exception as exc:  # a broken run must never kill the polling loop
            await self._say(chat, f"✗ {type(exc).__name__}: {str(exc)[:300]}")
        finally:
            chat.run_id, chat.status_id = None, None

    async def _on_event(self, chat: _Chat, event: AgentEvent) -> bool:
        """Render one event; returns True for a terminal event."""
        kind = event.type
        if kind is EventType.ASSISTANT_MESSAGE:
            content = str(event.data.get("content", "")).strip()
            if content:
                for part in split_message(content):
                    await self._say(chat, part)
        elif kind is EventType.TOOL_STARTED:
            chat.status_lines.append(f"→ {_summary(event)}")
            await self._flush_status(chat)
        elif kind is EventType.TOOL_COMPLETED and event.data.get("ok") is False:
            chat.status_lines.append(f"✗ {event.data.get('tool')} failed")
            await self._flush_status(chat)
        elif kind is EventType.APPROVAL_REQUIRED:
            await self._ask_approval(chat, event)
        elif kind is EventType.RUN_COMPLETED:
            await self._finish_status(chat, "✓ done")
            return True
        elif kind is EventType.RUN_FAILED:
            error = str(event.data.get("error", "run failed"))
            await self._finish_status(
                chat, "■ cancelled" if "RunCancelled" in error else "✗ failed"
            )
            if "RunCancelled" not in error:
                await self._say(chat, f"✗ {error[:600]}")
            return True
        return False

    async def _ask_approval(self, chat: _Chat, event: AgentEvent) -> None:
        approval_id = str(event.data["approval_id"])
        label = _summary(event)
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "✓ Approve", "callback_data": f"ap:{approval_id}:y"},
                    {"text": "✗ Reject", "callback_data": f"ap:{approval_id}:n"},
                ]
            ]
        }
        message_id = await self._api.send_message(chat.chat_id, f"Approve?\n{label}", keyboard)
        chat.approvals[approval_id] = (event.run_id, event.session_id, message_id, label)

    # ── status message (debounced) and sending ───────────────────────────────
    def _status_text(self, chat: _Chat, header: str) -> str:
        lines = chat.status_lines[-_STATUS_LINES:]
        return "\n".join([header, *lines])

    async def _flush_status(self, chat: _Chat) -> None:
        """Edit the status message, but at most once per ``edit_interval`` (Telegram rate limits)."""
        if chat.status_id is None or self._clock() - chat.last_edit < self._edit_interval:
            return
        chat.last_edit = self._clock()
        await self._edit(chat.chat_id, chat.status_id, self._status_text(chat, "⏳ working…"))

    async def _finish_status(self, chat: _Chat, header: str) -> None:
        if chat.status_id is not None:
            await self._edit(chat.chat_id, chat.status_id, self._status_text(chat, header))

    async def _edit(self, chat_id: int, message_id: int, text: str) -> None:
        # "message is not modified", a deleted message, a rate limit: cosmetic only.
        with contextlib.suppress(TelegramError):
            await self._api.edit_message_text(chat_id, message_id, text)

    async def _say(self, chat: _Chat, text: str, markup: dict[str, Any] | None = None) -> None:
        # The user may have blocked the bot; that must never stop the gateway.
        with contextlib.suppress(TelegramError):
            await self._api.send_message(chat.chat_id, text, markup)
