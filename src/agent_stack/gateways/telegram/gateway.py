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
import html
import json
import logging
import math
import os
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from agent_stack.app.application import Application, LineTurn, redact_secrets
from agent_stack.core.events import AgentEvent, EventType
from agent_stack.gateways.base import (
    HistoryItem,
    MessageEnvelope,
    SessionSummary,
    SessionUsage,
    format_cost,
    model_breakdown,
    usage_summary,
)
from agent_stack.gateways.ratelimit import RateLimiter
from agent_stack.gateways.telegram.api import TelegramApi, TelegramError
from agent_stack.gateways.telegram.render import (
    escape_fit,
    format_tool_detail,
    to_html,
    tool_label,
)

__all__ = ["TelegramConfigError", "TelegramGateway", "TelegramSettings", "split_message"]

_LOG = logging.getLogger("agent_stack.telegram")
#: A stranger (or a group) is logged at most once per this many seconds, per sender and reason.
_DENIED_LOG_SECONDS = 60.0
#: Rate limits are per this many seconds; a chat over one hears "slow down" once per window.
_RATE_WINDOW = 60.0

#: Telegram rejects messages longer than this many characters.
MESSAGE_LIMIT = 4096
_STATUS_LINES = 6
_SEEN_LIMIT = 2000
_PAGE = 6  # conversations per page of /sessions
_LISTING = 60  # how many recent conversations /sessions can page through
_RECAP = 4  # messages replayed when a conversation is opened
_MODES = ("off", "compact", "full")
_REWIND_CHOICES = 8  # recent messages offered by /rewind
_COMMANDS = [
    ("new", "Start a fresh conversation"),
    ("sessions", "Browse and open conversations"),
    ("history", "Show the latest messages of this conversation"),
    ("tools", "Inspect the tool calls of the last turn"),
    ("verbose", "How much tool activity to show: off, compact, full"),
    ("rename", "Rename this conversation"),
    ("delete", "Delete this conversation"),
    ("rewind", "Go back to an earlier message (code, conversation or both)"),
    ("branches", "Draw this conversation's branches"),
    ("ref", "Share this point, or open a shared one: /ref [asref://…]"),
    ("files", "Put this conversation's files in the working folder"),
    ("mode", "Approval mode: /mode auto or /mode ask"),
    ("model", "Show or switch the model: /model provider:name"),
    ("compact", "Summarise the conversation to free context: /compact [focus]"),
    ("export", "Send this conversation as a file: /export [md|json]"),
    ("cost", "What this conversation has cost so far"),
    ("mcp", "MCP servers: state and tool counts"),
    ("status", "Where you are and what is running"),
    ("cancel", "Stop the current run"),
    ("help", "Show this list"),
]
_HELP = (
    "Send a message to talk to the agent.\n\n"
    "/new  start a fresh conversation\n"
    "/sessions  browse and open any conversation (also the ones from the terminal)\n"
    "/history [n]  the latest messages of this conversation\n"
    "/tools  inspect the tool calls of the last turn: arguments and results\n"
    "/verbose [off|compact|full]  how much tool activity to show while it works\n"
    "/rename <title>  rename this conversation\n"
    "/delete  delete this conversation (asks first)\n"
    "/rewind  go back to an earlier message; restores the code, the conversation, or both\n"
    "/branches  draw this conversation's branches\n"
    "/ref [ref]  make a reference to this point, or open one from another chat\n"
    "/files  put this conversation's files in the working folder\n"
    "/mode [ask|auto]  who approves tool calls: you, or a classifier that asks you when unsure\n"
    "/model [provider:name]  show the model, or switch it for the next messages\n"
    "/compact [focus]  summarise the conversation so far to free context\n"
    "/export [md|json]  this conversation as a file (Markdown by default)\n"
    "/<skill> [request]  run a skill (type / to see them)\n"
    "/cost  what this conversation has cost (tokens and dollars)\n"
    "/mcp  the MCP servers, their state and tool counts\n"
    "/status  where you are and what is running\n"
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
    #: Where each chat's open conversation and tool-detail mode survive a restart (optional).
    state_path: Path | None = None

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
        state = env.get("AGENT_STACK_TELEGRAM_STATE")
        return cls(
            token,
            users,
            env.get("AGENT_STACK_TELEGRAM_PRINCIPAL") or None,
            Path(state).expanduser() if state else None,
        )


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
    mode: str = "compact"  # how much tool activity a run shows: off, compact, full
    restored: bool = False  # session came from the state file and is not yet checked
    run_id: str | None = None
    task: asyncio.Task[None] | None = None
    status_id: int | None = None
    status_lines: list[str] = field(default_factory=list)
    tool_count: int = 0
    run_usage: SessionUsage = field(default_factory=SessionUsage)  # this run's spend (live)
    usage_before: SessionUsage | None = None  # the conversation's total when the run began
    full_ids: dict[str, int] = field(default_factory=dict)  # tool_call_id -> message (full mode)
    last_edit: float = 0.0
    listing: list[SessionSummary] = field(default_factory=list)
    tools: list[HistoryItem] = field(default_factory=list)  # snapshot behind the /tools buttons
    approvals: dict[str, tuple[str, str, int, str]] = field(default_factory=dict)
    rewind: list[LineTurn] = field(default_factory=list)  # snapshot behind the /rewind buttons

    @property
    def busy(self) -> bool:
        return self.task is not None and not self.task.done()


def _summary(event: AgentEvent) -> str:
    """One status line for a tool call: its command, else its first short argument."""
    return tool_label(str(event.data.get("tool", "tool")), _args(event), 120)


def _args(event: AgentEvent) -> Mapping[str, Any] | None:
    return _redacted_args(event.data.get("args"))


def _redacted_args(args: Any) -> Mapping[str, Any] | None:
    """Tool arguments with secrets removed: everything shown here goes to Telegram's servers."""
    return _redact(args) if isinstance(args, Mapping) else None


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, Mapping):
        return {str(k): _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


def _button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


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
        self._saved = self._read_state()
        self._denied_at: dict[tuple[int, str], float] = {}
        config = application.config
        self._message_limit = RateLimiter(config.rate_messages, _RATE_WINDOW, clock)
        self._button_limit = RateLimiter(config.rate_requests, _RATE_WINDOW, clock)
        self._slowed_at: dict[int, float] = {}

    # ── persisted chat state ─────────────────────────────────────────────────
    def _read_state(self) -> dict[str, dict[str, Any]]:
        path = self._settings.state_path
        if path is None:
            return {}
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_state(self) -> None:
        """Remember each chat's open conversation across restarts (best effort, atomic)."""
        path = self._settings.state_path
        if path is None:
            return
        for chat in self._chats.values():
            self._saved[str(chat.chat_id)] = {
                "session": chat.session,
                "title": chat.title,
                "mode": chat.mode,
            }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(self._saved), "utf-8")
            os.replace(temp, path)
        except OSError:
            pass  # remembering is a convenience; never fail a chat over it

    # ── polling loop ─────────────────────────────────────────────────────────
    def _command_menu(self) -> list[tuple[str, str]]:
        """The ``/`` menu Telegram shows: the built-ins, then one entry per skill.

        Telegram command names are lower-case letters, digits and underscores (up to 32), so a
        skill called ``system-data-mapping`` appears as ``/system_data_mapping``; the application
        reads the underscores as hyphens. Telegram allows 100 commands in all.
        """
        menu = list(_COMMANDS)
        taken = {name for name, _ in menu}
        for command in self._app.slash_commands():
            name = re.sub(r"[^a-z0-9_]", "_", command.name.lower())[:32]
            if name and name not in taken:
                taken.add(name)
                menu.append((name, command.description[:200] or "Run this skill"))
        return menu[:100]

    async def run(self) -> None:
        """Poll until :meth:`stop`; transient network errors back off instead of ending it."""
        with contextlib.suppress(TelegramError):
            await self._api.set_my_commands(self._command_menu())
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
        if not message:
            return
        sender = int(message.get("from", {}).get("id", 0))
        if not self._is_owner_chat(sender, message.get("chat"), "message"):
            return
        chat_id = int(message["chat"]["id"])
        if await self._too_fast(self._message_limit, chat_id):
            return
        if "text" not in message:
            await self._say(self._chat(chat_id, sender), "I only understand text messages for now.")
            return
        key = f"message:{chat_id}:{message.get('message_id')}"
        if not self._first_time(key):
            return
        await self._on_text(self._chat(chat_id, sender), str(message["text"]).strip(), key)

    def _is_owner_chat(self, sender: int, chat: Mapping[str, Any] | None, what: str) -> bool:
        """Serve only an allow-listed user, and only in their own private chat.

        A bot can be added to groups and channels. Replying there would show a conversation (tool
        output, file contents) to everyone in them, so anything that is not the owner's private
        chat is ignored. Refusals are silent to the sender and logged locally for the owner.
        """
        if sender not in self._settings.allowed_users:
            self._note_denied(sender, f"{what} from a user who is not on the allow-list")
            return False
        chat = chat or {}
        if chat.get("type") != "private" or int(chat.get("id", 0) or 0) != sender:
            self._note_denied(sender, f"{what} outside the owner's private chat")
            return False
        return True

    def _note_denied(self, sender: int, reason: str) -> None:
        """Log a refusal (id and reason only, never content), at most once a minute per sender."""
        now = self._clock()
        key = (sender, reason)
        last = self._denied_at.get(key)
        if last is not None and now - last < _DENIED_LOG_SECONDS:
            return
        self._denied_at[key] = now
        _LOG.warning("ignored a %s (telegram user id %s)", reason, sender)

    async def _too_fast(self, limiter: RateLimiter, chat_id: int) -> bool:
        """Over the chat's rate: say so at most once a window, and drop the update."""
        wait = limiter.take(str(chat_id))
        if not wait:
            return False
        now = self._clock()
        last = self._slowed_at.get(chat_id)
        if last is None or now - last >= _RATE_WINDOW:
            self._slowed_at[chat_id] = now
            with contextlib.suppress(TelegramError):
                await self._api.send_message(
                    chat_id, f"Slow down: try again in {math.ceil(wait)}s."
                )
        return True

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
            saved = self._saved.get(str(chat_id))
            if saved:
                mode = saved.get("mode")
                chat.mode = mode if mode in _MODES else chat.mode
                if isinstance(saved.get("session"), str):
                    chat.session, chat.restored = saved["session"], True
                    chat.title = saved.get("title") if isinstance(saved.get("title"), str) else None
        return chat

    async def _check_restored(self, chat: _Chat) -> None:
        """A conversation remembered from the state file may have been deleted meanwhile."""
        if chat.restored and chat.session is not None:
            chat.restored = False
            if await self._app.get_session(chat.session) is None:
                chat.session, chat.title = None, None
                self._save_state()

    # ── text and commands ────────────────────────────────────────────────────
    async def _on_text(self, chat: _Chat, text: str, key: str) -> None:
        if not text:
            return
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()  # "/new@mybot" in group chats
        argument = argument.strip()
        if command in ("/start", "/help"):
            await self._say(chat, _HELP)
        elif command == "/new":
            await self._cmd_new(chat)
        elif command == "/sessions":
            await self._cmd_sessions(chat)
        elif command == "/cancel":
            await self._cmd_cancel(chat)
        elif command == "/history":
            await self._cmd_history(chat, argument)
        elif command == "/tools":
            await self._cmd_tools(chat)
        elif command == "/verbose":
            await self._cmd_verbose(chat, argument)
        elif command == "/rename":
            await self._cmd_rename(chat, argument)
        elif command == "/delete":
            await self._cmd_delete(chat)
        elif command == "/rewind":
            await self._cmd_rewind(chat)
        elif command == "/branches":
            await self._cmd_branches(chat)
        elif command == "/ref":
            await self._cmd_ref(chat, argument)
        elif command == "/files":
            await self._cmd_files(chat)
        elif command == "/mode":
            await self._cmd_mode(chat, argument)
        elif command == "/model":
            await self._cmd_model(chat, argument)
        elif command == "/compact":
            await self._cmd_compact(chat, argument)
        elif command == "/export":
            await self._cmd_export(chat, argument)
        elif command == "/cost":
            await self._cmd_cost(chat)
        elif command == "/status":
            await self._cmd_status(chat)
        elif command == "/mcp":
            servers = await self._app.mcp_servers()
            lines = [s.line() for s in servers] or ["No MCP servers (add them to .mcp.json)."]
            await self._say(chat, "\n".join(lines))
        elif chat.busy:
            await self._queue_input(chat, text)
        else:
            chat.task = asyncio.create_task(self._run_message(chat, text, key))

    async def _queue_input(self, chat: _Chat, text: str) -> None:
        """Send a message into the working run; the model reads it before its next step."""
        if text.startswith("/"):
            await self._say(chat, "A run is in progress. Send /cancel to stop it first.")
        elif chat.run_id is not None and await self._app.send_input(chat.run_id, text):
            await self._say(chat, "Queued: the agent reads it at its next step.")
        else:
            await self._say(
                chat, "Could not add that to the run (starting or just done). Send it again."
            )

    async def _refuse_if_busy(self, chat: _Chat) -> bool:
        if chat.busy:
            await self._say(chat, "A run is in progress. Send /cancel to stop it first.")
        return chat.busy

    async def _cmd_new(self, chat: _Chat) -> None:
        if await self._refuse_if_busy(chat):
            return
        chat.session, chat.title = None, None  # a draft, stored on the first message
        self._save_state()
        await self._say(chat, "New conversation. What would you like to do?")

    # ── history: rewind, branches, references ────────────────────────────────
    async def _cmd_rewind(self, chat: _Chat) -> None:
        if await self._refuse_if_busy(chat):
            return
        if chat.session is None:
            await self._say(chat, "Nothing to rewind to yet.")
            return
        try:
            turns = [t for t in await self._app.timeline.lineage(chat.session) if t.turn.user_text]
        except Exception as exc:
            await self._say(chat, f"Cannot rewind here: {exc}")
            return
        if not turns:
            await self._say(chat, "Nothing to rewind to yet.")
            return
        chat.rewind = turns[-_REWIND_CHOICES:]
        rows = [
            [_button(f"{item.turn.user_text.strip().splitlines()[0][:40]}", f"rw:{i}")]
            for i, item in reversed(list(enumerate(chat.rewind)))
        ]
        await self._say(
            chat,
            "Go back to just before which message? (newest first)",
            {"inline_keyboard": rows},
        )

    async def _on_rewind(self, chat: _Chat, callback_id: str, message_id: int, data: str) -> None:
        parts = data.split(":")
        try:
            target = chat.rewind[int(parts[1])]
        except (ValueError, IndexError):
            await self._api.answer_callback_query(
                callback_id, "That list is out of date. Send /rewind."
            )
            return
        if chat.busy or chat.session is None:
            await self._api.answer_callback_query(callback_id, "A run is in progress.")
            return
        if parts[0] == "rw":  # step two: what to restore
            await self._api.answer_callback_query(callback_id)
            i = parts[1]
            keyboard = {
                "inline_keyboard": [
                    [_button("Code and conversation", f"rm:{i}:b")],
                    [_button("Conversation only", f"rm:{i}:c")],
                    [_button("Code only", f"rm:{i}:k")],
                    [_button("Never mind", f"rm:{i}:x")],
                ]
            }
            await self._edit(
                chat.chat_id,
                message_id,
                f"Rewind to before “{target.turn.user_text.strip()[:60]}”. Restore what?",
                keyboard,
            )
            return
        mode = parts[2] if len(parts) > 2 else "x"
        if mode == "x":
            await self._api.answer_callback_query(callback_id, "Kept")
            await self._edit(chat.chat_id, message_id, "Rewind cancelled.")
            return
        try:
            outcome = await self._app.timeline.rewind_to(
                chat.session, target, conversation=mode in ("b", "c"), code=mode in ("b", "k")
            )
        except Exception as exc:
            await self._api.answer_callback_query(callback_id, "Rewind failed")
            await self._edit(chat.chat_id, message_id, f"Rewind failed: {exc}"[:300])
            return
        await self._api.answer_callback_query(callback_id, "Rewound")
        if outcome.session_id != chat.session:
            chat.session, chat.title, chat.restored = outcome.session_id, "rewound", False
            self._save_state()
        moved = len(outcome.files.moved)
        text = "Rewound. " + (f"{moved} file(s) restored." if moved else "No files changed.")
        if outcome.files.conflicts:
            text += f" Left alone (changed since): {', '.join(outcome.files.conflicts)}."
        await self._edit(chat.chat_id, message_id, text)
        if mode in ("b", "c"):
            await self._say(chat, "Your message, to edit and send again:")
            await self._say(chat, outcome.prompt)

    async def _cmd_branches(self, chat: _Chat) -> None:
        if chat.session is None:
            await self._say(chat, "No conversation yet.")
            return
        try:
            text = await self._app.timeline.tree_text(chat.session)
        except Exception as exc:
            await self._say(chat, f"Cannot draw the branches: {exc}")
            return
        body = html.escape(text)
        await self._say_html(chat, f"<pre>{body}</pre>", raw=text)

    async def _cmd_ref(self, chat: _Chat, argument: str) -> None:
        if await self._refuse_if_busy(chat):
            return
        timeline = self._app.timeline
        try:
            if argument:  # open a reference from another chat
                opened = await timeline.open_reference(argument)
                chat.session, chat.title, chat.restored = opened, "from a reference", False
                self._save_state()
                await self._say(
                    chat,
                    "Opened a new conversation from that point. Its files are not in the "
                    "folder yet: send /files to put them there.",
                )
                return
            if chat.session is None:
                await self._say(chat, "No conversation yet.")
                return
            done = [t for t in await timeline.turns(chat.session) if t.completed_event_id]
            if not done:
                await self._say(chat, "Nothing has finished in this conversation yet.")
                return
            ref = await timeline.reference(chat.session, str(done[-1].index))
        except Exception as exc:
            await self._say(chat, f"Error: {exc}")
            return
        await self._say_html(
            chat,
            f"Reference to the last finished turn (paste it into another chat with /ref):\n"
            f"<code>{html.escape(ref)}</code>",
            raw=ref,
        )

    async def _cmd_compact(self, chat: _Chat, argument: str) -> None:
        if chat.session is None:
            await self._say(chat, "Nothing to compact yet.")
            return
        if chat.busy:  # compacting waits for the run; do not hold up this chat's updates
            await self._say(chat, "A run is going. Compact after it finishes, or /cancel it.")
            return
        try:
            count = await self._app.compact_session(chat.session, argument)
        except Exception as exc:
            await self._say(chat, f"Cannot compact: {redact_secrets(str(exc))[:300]}")
            return
        await self._say(
            chat,
            f"Compacted {count} messages into a summary; the next message starts from it."
            if count
            else "Nothing new to compact.",
        )

    async def _cmd_export(self, chat: _Chat, argument: str) -> None:
        if chat.session is None:
            await self._say(chat, "Nothing to export yet.")
            return
        fmt = argument.lower() or "md"
        if fmt not in ("md", "json"):
            await self._say(chat, "Usage: /export [md|json]")
            return
        try:
            text = await self._app.export_session(chat.session, fmt)
            name = await self._app.export_filename(chat.session, fmt)
            await self._api.send_document(chat.chat_id, name, text.encode("utf-8"))
        except Exception as exc:
            await self._say(chat, f"Cannot export: {redact_secrets(str(exc))[:300]}")

    async def _cmd_model(self, chat: _Chat, argument: str) -> None:
        if not argument.strip():
            await self._say(chat, f"Model: {self._app.get_model()}.\nSwitch: /model provider:name")
            return
        try:
            now = self._app.set_model(argument)
        except Exception as exc:
            await self._say(chat, f"Cannot switch model: {redact_secrets(str(exc))[:300]}")
            return
        await self._say(chat, f"Model: {now} (from the next message on).")

    async def _cmd_mode(self, chat: _Chat, argument: str) -> None:
        current = self._app.get_approval_mode()
        wanted = argument.strip().lower()
        if not wanted:
            keyboard = {
                "inline_keyboard": [
                    [_button("Ask me" + (" ✓" if current == "ask" else ""), "am:ask")],
                    [_button("Auto" + (" ✓" if current == "auto" else ""), "am:auto")],
                ]
            }
            await self._say(
                chat,
                f"Approval mode: {current}.\nask: you approve every gated action.\n"
                "auto: a classifier decides what is safe and asks you only when unsure.",
                keyboard,
            )
            return
        try:
            now = self._app.set_approval_mode(wanted)
        except Exception:
            await self._say(chat, "Use /mode ask or /mode auto.")
            return
        await self._say(chat, f"Approval mode: {now}.")

    async def _cmd_files(self, chat: _Chat) -> None:
        if await self._refuse_if_busy(chat):
            return
        if chat.session is None:
            await self._say(chat, "No conversation yet.")
            return
        try:
            result = await self._app.timeline.restore_files(chat.session)
        except Exception as exc:
            await self._say(chat, f"Could not restore files: {exc}")
            return
        text = f"{len(result.moved)} file(s) restored." if result.moved else "Files already match."
        if result.conflicts:
            text += f" Left alone (changed since): {', '.join(result.conflicts)}."
        await self._say(chat, text)

    async def _cmd_cancel(self, chat: _Chat) -> None:
        if not chat.busy or chat.run_id is None:
            await self._say(chat, "Nothing is running.")
            return
        await self._app.cancel_run(chat.run_id)

    async def _cmd_sessions(self, chat: _Chat) -> None:
        if await self._refuse_if_busy(chat):
            return
        chat.listing = await self._app.list_sessions(chat.principal, limit=_LISTING)
        if not chat.listing:
            await self._say(chat, "No conversations yet.")
            return
        text, keyboard = self._sessions_page(chat, 0)
        await self._say(chat, text, keyboard)

    def _sessions_page(self, chat: _Chat, page: int) -> tuple[str, dict[str, Any]]:
        pages = max((len(chat.listing) + _PAGE - 1) // _PAGE, 1)
        page = min(max(page, 0), pages - 1)
        start = page * _PAGE
        rows = [
            [_button(("● " if s.id == chat.session else "") + s.title[:40], f"sw:{start + i}")]
            for i, s in enumerate(chat.listing[start : start + _PAGE])
        ]
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(_button("◀ Newer", f"sp:{page - 1}"))
            nav.append(_button(f"{page + 1}/{pages}", "noop"))
            if page < pages - 1:
                nav.append(_button("Older ▶", f"sp:{page + 1}"))
            rows.append(nav)
        return "Conversations (tap to switch):", {"inline_keyboard": rows}

    async def _cmd_history(self, chat: _Chat, argument: str) -> None:
        if chat.session is None:
            await self._say(chat, "No conversation is open. Send a message or use /sessions.")
            return
        count = min(max(int(argument), 1), 20) if argument.isdigit() else 6
        history = await self._app.session_history(chat.session)
        shown = [h for h in history if h.kind != "tool"][-count:]
        if not shown:
            await self._say(chat, "This conversation has no messages yet.")
            return
        await self._say_html(chat, self._recap_html(shown, 700))

    @staticmethod
    def _recap_html(items: list[HistoryItem], limit: int) -> str:
        out = []
        for h in items:
            if h.kind == "compaction":
                out.append("<i>↺ context compacted: earlier messages were summarised</i>")
                continue
            who = "<b>You</b>" if h.kind == "user" else "<b>Agent</b>"
            text = h.text if len(h.text) <= limit else h.text[: limit - 1] + "…"
            out.append(f"{who}\n{to_html(text)}")
        return "\n\n".join(out)

    async def _cmd_tools(self, chat: _Chat) -> None:
        if chat.session is None:
            await self._say(chat, "No conversation is open. Send a message or use /sessions.")
            return
        await self._show_tools(chat, None)

    async def _cmd_verbose(self, chat: _Chat, argument: str) -> None:
        wanted = argument.lower()
        if wanted in _MODES:
            chat.mode = wanted
            self._save_state()
            await self._say(chat, f"Tool activity: {wanted}.")
            return
        rows = [[_button(("● " if m == chat.mode else "") + m, f"vb:{m}") for m in _MODES]]
        await self._say(
            chat,
            "How much tool activity should a run show?\n"
            "off: only the answer. compact: one live status message. "
            "full: every call with its arguments, as its own message.",
            {"inline_keyboard": rows},
        )

    async def _cmd_rename(self, chat: _Chat, argument: str) -> None:
        if chat.session is None:
            await self._say(chat, "No conversation is open yet.")
        elif not argument:
            await self._say(chat, "Usage: /rename <new title>")
        else:
            await self._app.rename_session(chat.session, argument[:80])
            chat.title = argument[:80]
            self._save_state()
            await self._say(chat, f"Renamed to “{chat.title}”.")

    async def _cmd_delete(self, chat: _Chat) -> None:
        if await self._refuse_if_busy(chat):
            return
        if chat.session is None:
            await self._say(chat, "No conversation is open.")
            return
        keyboard = {
            "inline_keyboard": [[_button("🗑 Delete it", "dl:y"), _button("Keep it", "dl:n")]]
        }
        await self._say(chat, f"Delete “{chat.title or 'Untitled'}” for good?", keyboard)

    async def _usage(self, chat: _Chat) -> SessionUsage | None:
        if chat.session is None:
            return None
        try:
            return await self._app.session_usage(chat.session)
        except Exception:  # cost is informational; never break a command over it
            return None

    async def _run_spend(self, chat: _Chat) -> SessionUsage:
        """This run's spend: the durable total now minus before (it includes subagents' calls,
        whose events never reach this chat), else what the live events added up to."""
        after = await self._usage(chat)
        before = chat.usage_before
        if after is None or before is None:
            return chat.run_usage
        return SessionUsage(
            calls=after.calls - before.calls,
            priced_calls=after.priced_calls - before.priced_calls,
            input_tokens=after.input_tokens - before.input_tokens,
            output_tokens=after.output_tokens - before.output_tokens,
            reasoning_tokens=after.reasoning_tokens - before.reasoning_tokens,
            cache_read_tokens=after.cache_read_tokens - before.cache_read_tokens,
            cache_write_tokens=after.cache_write_tokens - before.cache_write_tokens,
            cost_usd=after.cost_usd - before.cost_usd,
        )

    async def _cmd_cost(self, chat: _Chat) -> None:
        usage = await self._usage(chat)
        if usage is None:
            await self._say(chat, "No conversation is open yet, so nothing has been spent.")
            return
        lines = [
            f"Conversation: {usage_summary(usage)}",
            f"By model: {model_breakdown(usage)}" if model_breakdown(usage) else "",
            f"Reasoning tokens: {usage.reasoning_tokens:,}" if usage.reasoning_tokens else "",
        ]
        budget = self._app.config.budget_usd
        if budget is not None:
            lines.append(f"Budget: {format_cost(usage.cost_usd)} of {format_cost(budget)}")
        if chat.busy:
            lines.append(f"This run so far: {usage_summary(await self._run_spend(chat))}")
        await self._say(chat, "\n".join(line for line in lines if line))

    async def _cmd_status(self, chat: _Chat) -> None:
        state = "running" if chat.busy else "idle"
        where = f"“{chat.title or 'Untitled'}”" if chat.session else "a new draft"
        usage = await self._usage(chat)
        spent = f"\nSpent: {usage_summary(usage)}" if usage and usage.calls else ""
        await self._say(
            chat,
            f"Conversation: {where}\nRun: {state}\nTool activity: {chat.mode}\n"
            f"Identity: {chat.principal}{spent}",
        )

    # ── callbacks: switching, paging, tools, approvals ───────────────────────
    async def _on_callback(self, callback: Mapping[str, Any]) -> None:
        sender = int(callback.get("from", {}).get("id", 0))
        message = callback.get("message") or {}
        if not self._is_owner_chat(sender, message.get("chat"), "button"):
            return
        if await self._too_fast(self._button_limit, int(message["chat"]["id"])):
            return
        chat = self._chat(int(message["chat"]["id"]), sender)
        data = str(callback.get("data", ""))
        callback_id = str(callback.get("id", ""))
        message_id = int(message.get("message_id", 0))
        prefix = data.split(":", 1)[0]
        if prefix == "ap":
            await self._on_approval(chat, callback_id, data)
        elif prefix == "sw":
            await self._on_switch(chat, callback_id, data)
        elif prefix == "sp":
            await self._on_page(chat, callback_id, message_id, data)
        elif prefix in ("tl", "td"):
            await self._on_tools(chat, callback_id, message_id, data)
        elif prefix == "dl":
            await self._on_delete(chat, callback_id, message_id, data)
        elif prefix == "vb":
            await self._on_verbose(chat, callback_id, message_id, data)
        elif prefix == "am":
            try:
                now = self._app.set_approval_mode(data.split(":", 1)[1])
            except Exception:
                await self._api.answer_callback_query(callback_id, "No such mode")
                return
            await self._api.answer_callback_query(callback_id, f"Mode: {now}")
            await self._edit(chat.chat_id, message_id, f"Approval mode: {now}.")
        elif prefix in ("rw", "rm"):
            await self._on_rewind(chat, callback_id, message_id, data)
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
        chat.session, chat.title, chat.restored = target.id, target.title, False
        self._save_state()
        await self._api.answer_callback_query(callback_id, "Switched")
        history = await self._app.session_history(target.id)
        conversation = [h for h in history if h.kind in ("user", "assistant")]
        calls = sum(1 for h in history if h.kind == "tool")
        recap = [
            f"{'You' if h.kind == 'user' else 'Agent'}: {h.text[:300]}" for h in conversation[-2:]
        ]
        actions = [_button("📜 History", "hs")]
        if calls:
            actions.insert(0, _button(f"🔧 Tool calls ({calls})", "tl:"))
        actions.append(_button("🗑 Delete", "dl:ask"))
        await self._say(
            chat,
            f"Switched to “{target.title}”.\n\n" + "\n\n".join(recap),
            {"inline_keyboard": [actions]},
        )

    async def _on_page(self, chat: _Chat, callback_id: str, message_id: int, data: str) -> None:
        await self._api.answer_callback_query(callback_id)
        if not chat.listing:
            return
        try:
            page = int(data.split(":", 1)[1])
        except ValueError:
            return
        text, keyboard = self._sessions_page(chat, page)
        await self._edit(chat.chat_id, message_id, text, keyboard)

    async def _on_verbose(self, chat: _Chat, callback_id: str, message_id: int, data: str) -> None:
        mode = data.split(":", 1)[1]
        if mode not in _MODES:
            await self._api.answer_callback_query(callback_id)
            return
        chat.mode = mode
        self._save_state()
        await self._api.answer_callback_query(callback_id, f"Tool activity: {mode}")
        await self._edit(chat.chat_id, message_id, f"Tool activity: {mode}.")

    async def _on_delete(self, chat: _Chat, callback_id: str, message_id: int, data: str) -> None:
        verdict = data.split(":", 1)[1]
        if verdict == "ask":
            await self._api.answer_callback_query(callback_id)
            await self._cmd_delete(chat)
            return
        if chat.busy or chat.session is None:
            await self._api.answer_callback_query(callback_id, "Nothing to delete right now.")
            return
        if verdict != "y":
            await self._api.answer_callback_query(callback_id, "Kept")
            await self._edit(chat.chat_id, message_id, "Kept.")
            return
        title = chat.title or "Untitled"
        await self._app.delete_session(chat.session)
        chat.session, chat.title, chat.tools = None, None, []
        self._save_state()
        await self._api.answer_callback_query(callback_id, "Deleted")
        await self._edit(chat.chat_id, message_id, f"🗑 Deleted “{title}”.")

    async def _show_tools(self, chat: _Chat, message_id: int | None) -> None:
        """List the last turn's tool calls as buttons (new message, or edit the one tapped)."""
        history = await self._app.session_history(chat.session or "")
        last_user = max((i for i, h in enumerate(history) if h.kind == "user"), default=-1)
        chat.tools = [
            replace(h, args=_redacted_args(h.args))
            for h in history[last_user + 1 :]
            if h.kind == "tool"
        ]
        if not chat.tools:
            text, keyboard = "The last turn made no tool calls.", None
        else:
            rows = [
                [_button(tool_label(t.text, t.args, 40), f"td:{i}")]
                for i, t in enumerate(chat.tools[:30])
            ]
            text = f"{len(chat.tools)} tool call(s) in the last turn. Tap one for details:"
            keyboard = {"inline_keyboard": rows}
        if message_id is None:
            await self._say(chat, text, keyboard)
        else:
            await self._edit(chat.chat_id, message_id, text, keyboard)

    async def _on_tools(self, chat: _Chat, callback_id: str, message_id: int, data: str) -> None:
        await self._api.answer_callback_query(callback_id)
        if chat.session is None:
            return
        if data == "tl:":
            await self._show_tools(chat, None)  # a new message: the run's status stays put
            return
        try:
            item = chat.tools[int(data.split(":", 1)[1])]
        except (ValueError, IndexError):
            await self._say(chat, "That list is out of date. Send /tools.")
            return
        back = {"inline_keyboard": [[_button("◀ All tool calls", "tl:")]]}
        await self._say_html(chat, format_tool_detail(item), back)

    # ── one run ──────────────────────────────────────────────────────────────
    async def _run_message(self, chat: _Chat, text: str, key: str) -> None:
        try:
            await self._check_restored(chat)
            if chat.session is None:
                chat.session = await self._app.new_session(chat.principal)
            if chat.title is None:
                chat.title = text[:80]
                await self._app.set_session_title(chat.session, chat.title)
            self._save_state()
            with_typing = asyncio.create_task(self._api.send_chat_action(chat.chat_id))
            chat.status_lines, chat.tool_count, chat.full_ids = [], 0, {}
            chat.run_usage = SessionUsage()
            chat.usage_before = await self._usage(chat)
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
                for part in split_message(content, 3500):
                    await self._say_html(chat, to_html(part), raw=part)
        elif kind is EventType.MODEL_USAGE:
            chat.run_usage = chat.run_usage.plus(event.data)  # a subagent's calls count too
        elif kind is EventType.TOOL_STARTED:
            chat.tool_count += 1
            await self._tool_started(chat, event)
        elif kind is EventType.TOOL_COMPLETED:
            await self._tool_completed(chat, event)
        elif kind is EventType.APPROVAL_AUTO:
            await self._auto_decision(chat, event)
        elif kind is EventType.CONTEXT_COMPACTED:
            chat.status_lines.append(
                f"↺ context compacted ({event.data.get('messages', 0)} messages)"
            )
        elif kind is EventType.APPROVAL_REQUIRED:
            await self._ask_approval(chat, event)
        elif kind is EventType.RUN_COMPLETED:
            await self._finish_status(chat, "✓ done")
            return True
        elif kind is EventType.RUN_FAILED:
            error = redact_secrets(str(event.data.get("error", "run failed")))
            await self._finish_status(
                chat, "■ cancelled" if "RunCancelled" in error else "✗ failed"
            )
            if "RunCancelled" not in error:
                await self._say(chat, f"✗ {error[:600]}")
            return True
        return False

    async def _tool_started(self, chat: _Chat, event: AgentEvent) -> None:
        if chat.mode == "full":
            call_id = str(event.data.get("tool_call_id", ""))
            args = json.dumps(dict(_args(event) or {}), ensure_ascii=False, default=str)
            with contextlib.suppress(TelegramError):
                chat.full_ids[call_id] = await self._api.send_message(
                    chat.chat_id,
                    f"🔧 <b>{escape_fit(str(event.data.get('tool', 'tool')), 80)}</b>\n"
                    f"<pre>{escape_fit(args, 1500)}</pre>",
                    None,
                    "HTML",
                )
        if chat.mode != "off":
            chat.status_lines.append(f"→ {_summary(event)}")
            await self._flush_status(chat)

    async def _tool_completed(self, chat: _Chat, event: AgentEvent) -> None:
        ok = event.data.get("ok") is not False
        if chat.mode == "full":
            message_id = chat.full_ids.get(str(event.data.get("tool_call_id", "")))
            if message_id is not None:
                error = redact_secrets(str(event.data.get("error") or ""))[:300]
                mark = "✓ ok" if ok else f"✗ failed{': ' + error if error else ''}"
                await self._edit(
                    chat.chat_id,
                    message_id,
                    f"🔧 <b>{escape_fit(str(event.data.get('tool', 'tool')), 80)}</b>  "
                    f"{escape_fit(mark, 400)}",
                    None,
                    "HTML",
                )
        if not ok and chat.mode != "off":
            chat.status_lines.append(f"✗ {event.data.get('tool')} failed")
            await self._flush_status(chat)

    async def _auto_decision(self, chat: _Chat, event: AgentEvent) -> None:
        """Say what auto mode did, so nothing is approved or blocked silently."""
        verdict = str(event.data.get("verdict"))
        tool = str(event.data.get("tool", "tool"))
        reason = redact_secrets(str(event.data.get("reason", "")))[:160]
        if verdict == "allow":
            line = f"✓ auto-approved {tool}: {reason}"
        elif verdict == "deny":
            line = f"⛔ auto mode blocked {tool}: {reason}"
        else:
            return  # "ask" is followed by the usual question, which shows the reason
        if chat.mode != "off":
            chat.status_lines.append(line)
            await self._flush_status(chat)
        if verdict == "deny":
            await self._say(chat, line)

    async def _ask_approval(self, chat: _Chat, event: AgentEvent) -> None:
        approval_id = str(event.data["approval_id"])
        label = _summary(event)
        args = json.dumps(dict(_args(event) or {}), ensure_ascii=False, indent=2, default=str)
        detail = args if len(args) <= 1500 else args[:1499] + "…"
        shown = escape_fit(detail, 2500)
        keyboard = {
            "inline_keyboard": [
                [
                    _button("✓ Approve", f"ap:{approval_id}:y"),
                    _button("✗ Reject", f"ap:{approval_id}:n"),
                ]
            ]
        }
        note = redact_secrets(str(event.data.get("note", "")))
        why = f"\n<i>Auto mode left this to you: {escape_fit(note, 200)}</i>" if note else ""
        message_id = await self._send_html(
            chat.chat_id,
            f"<b>Approve?</b>\n{escape_fit(label, 300)}\n<pre>{shown}</pre>{why}",
            keyboard,
            fallback=f"Approve?\n{label}\n{detail}" + (f"\nAuto mode: {note}" if note else ""),
        )
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
        if chat.status_id is None:
            return
        spent = await self._run_spend(chat)
        if spent.priced_calls:
            header = f"{header} · {format_cost(spent.cost_usd)}"
        keyboard = None
        if chat.tool_count:
            keyboard = {"inline_keyboard": [[_button(f"🔧 Tool calls ({chat.tool_count})", "tl:")]]}
        await self._edit(chat.chat_id, chat.status_id, self._status_text(chat, header), keyboard)

    async def _edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        # "message is not modified", a deleted message, a rate limit: cosmetic only.
        with contextlib.suppress(TelegramError):
            await self._api.edit_message_text(chat_id, message_id, text, markup, parse_mode)

    async def _say(self, chat: _Chat, text: str, markup: dict[str, Any] | None = None) -> None:
        # The user may have blocked the bot; that must never stop the gateway.
        with contextlib.suppress(TelegramError):
            await self._api.send_message(chat.chat_id, text, markup)

    async def _say_html(
        self,
        chat: _Chat,
        text: str,
        markup: dict[str, Any] | None = None,
        *,
        raw: str | None = None,
    ) -> None:
        """Send HTML; if Telegram rejects it, send the plain ``raw`` text instead."""
        plain = raw if raw is not None else text
        with contextlib.suppress(TelegramError):
            if len(text) > MESSAGE_LIMIT:  # escaping can grow a message past Telegram's limit
                await self._api.send_message(chat.chat_id, plain[:MESSAGE_LIMIT], markup)
            else:
                await self._send_html(chat.chat_id, text, markup, fallback=plain)

    async def _send_html(
        self, chat_id: int, text: str, markup: dict[str, Any] | None, *, fallback: str
    ) -> int:
        try:
            return await self._api.send_message(chat_id, text, markup, "HTML")
        except TelegramError as exc:
            if exc.code != 400:  # only a parse error is worth a plain retry
                raise
            return await self._api.send_message(chat_id, fallback, markup)
