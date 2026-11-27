"""MCP client: tools from Model Context Protocol servers become tools of the agent.

Servers are configured in the Claude Code shape, ``{"mcpServers": {name: {...}}}``, read from
``~/.agent-stack/mcp.json`` and ``<workspace>/.mcp.json`` (the workspace wins per server name).
A server is either ``{"command", "args", "env"}`` (stdio) or ``{"url", "headers"}`` (streamable
HTTP); ``"trusted": true`` lets its tools run without approval; ``"startTimeout"`` (seconds,
default 30) bounds its start. Values may use ``${VAR}``.

Each server is held open by its own task (the SDK's scopes must be entered and left in one task).
A server that cannot start or list its tools is logged and reported, never fatal. Its tools are
``mcp__<server>__<tool>``. A server whose connection is lost is restarted on the next call to one
of its tools (at most :data:`MAX_RESTARTS` times); ``notifications/tools/list_changed`` re-lists
its tools and hands the new set to the ``on_change`` callback.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

from agent_stack.core.images import image_block, make_image

__all__ = ["McpManager", "McpServerConfig", "McpServerStatus", "load_mcp_config"]

_LOG = logging.getLogger(__name__)
#: How long one server may take to start and list its tools before it is reported as failed.
START_TIMEOUT_SECONDS = 30.0
#: How often one server is restarted after losing its connection before it is given up.
MAX_RESTARTS = 3
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    trusted: bool = False
    start_timeout: float = START_TIMEOUT_SECONDS
    #: Why the entry cannot be used (bad shape); such a server is reported, never started.
    error: str | None = None

    @property
    def transport(self) -> str:
        return "http" if self.url else "stdio"


@dataclass
class McpServerStatus:
    name: str
    transport: str
    state: str = "not started"  # not started | starting | ready | failed | stopped
    tools: int = 0
    error: str | None = None

    def line(self) -> str:
        detail = (
            f"{self.tools} tool{'s' if self.tools != 1 else ''}" if self.state == "ready" else ""
        )
        why = f": {self.error}" if self.error else ""
        return f"{self.name} ({self.transport}) {self.state}{' ' + detail if detail else ''}{why}"


def _expand(value: str, environ: Mapping[str, str]) -> str:
    return _VAR.sub(lambda m: environ.get(m.group(1), ""), value)


def _parse(name: str, raw: Any, environ: Mapping[str, str]) -> McpServerConfig:
    if not isinstance(raw, dict):
        return McpServerConfig(name, error="entry is not an object")
    command, url = raw.get("command"), raw.get("url")
    if not isinstance(command, str) and not isinstance(url, str):
        return McpServerConfig(name, error="needs a 'command' or a 'url'")
    args, env, headers = raw.get("args", []), raw.get("env", {}), raw.get("headers", {})
    if not isinstance(args, list) or not isinstance(env, dict) or not isinstance(headers, dict):
        return McpServerConfig(name, error="'args' must be a list, 'env'/'headers' objects")
    timeout = raw.get("startTimeout", START_TIMEOUT_SECONDS)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        return McpServerConfig(name, error="'startTimeout' must be a positive number of seconds")
    return McpServerConfig(
        name=name,
        command=_expand(command, environ) if isinstance(command, str) else None,
        args=tuple(_expand(str(a), environ) for a in args),
        env={str(k): _expand(str(v), environ) for k, v in env.items()},
        url=_expand(url, environ)
        if isinstance(url, str) and not isinstance(command, str)
        else None,
        headers={str(k): _expand(str(v), environ) for k, v in headers.items()},
        trusted=raw.get("trusted") is True,
        start_timeout=float(timeout),
    )


def load_mcp_config(
    workspace: Path, home: Path, environ: Mapping[str, str]
) -> list[McpServerConfig]:
    """The configured servers: home ``mcp.json`` first, the workspace ``.mcp.json`` over it."""
    found: dict[str, McpServerConfig] = {}
    for path in (home / "mcp.json", workspace / ".mcp.json"):
        if not path.is_file():
            continue
        try:
            servers = json.loads(path.read_text("utf-8")).get("mcpServers", {})
            if not isinstance(servers, dict):
                raise ValueError("'mcpServers' is not an object")
        except (OSError, ValueError, AttributeError) as exc:
            _LOG.warning("ignoring MCP config %s: %s", path, exc)
            continue
        for name, raw in servers.items():
            found[str(name)] = _parse(str(name), raw, environ)
    return list(found.values())


def _text_of(result: Any) -> str:
    """A tool result as text: text blocks as they are, other blocks named by their type."""
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        parts.append(text if isinstance(text, str) else f"[{getattr(block, 'type', 'content')}]")
    if not parts and result.structured_content is not None:
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))
    return "\n".join(parts)


def _image_blocks(result: Any) -> list[dict[str, Any]]:
    """The result's images (P44) as model image blocks; an unusable one stays ``[image]`` text."""
    blocks = []
    for block in result.content:
        if getattr(block, "type", None) != "image":
            continue
        with contextlib.suppress(ValueError):
            image = make_image(base64.b64decode(str(block.data), validate=True))
            blocks.append(image_block(image.data, image.mime))
    return blocks


def _wrap(call_tool: Callable[..., Any], server: str, tool: Any) -> BaseTool:
    async def call(**kwargs: Any) -> str | list[Any]:
        result = await call_tool(server, tool.name, kwargs)
        text = _text_of(result)
        if result.is_error:
            raise RuntimeError(text or "the MCP tool reported an error")
        images = _image_blocks(result)
        return [*images, {"type": "text", "text": text}] if images else text

    schema = dict(tool.input_schema or {"type": "object", "properties": {}})
    schema.setdefault("properties", {})
    return StructuredTool(
        name=f"mcp__{_UNSAFE.sub('_', server)}__{_UNSAFE.sub('_', tool.name)}"[:64],
        description=(tool.description or f"{tool.name} (MCP server {server})")[:1024],
        args_schema=schema,
        coroutine=call,
    )


@dataclass
class _Held:
    """One server's connection, opened and left by its holder task."""

    config: McpServerConfig
    client: Any = None  # the open Client while the server is up, else None
    tools: list[BaseTool] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)  # stop, or re-list the tools
    stopping: bool = False
    restarts: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class McpManager:
    """Starts the configured servers, hands out their tools, restarts and stops them."""

    def __init__(self, servers: list[McpServerConfig], log_dir: Path) -> None:
        self._log_dir = log_dir
        self._status = {s.name: McpServerStatus(s.name, s.transport) for s in servers}
        self._trusted = {f"mcp__{_UNSAFE.sub('_', s.name)}__" for s in servers if s.trusted}
        self._held = {s.name: _Held(s) for s in servers if s.error is None}
        self._on_change: Callable[[list[BaseTool]], None] | None = None
        for server in servers:
            if server.error:
                self._fail(server.name, server.error)

    def status(self) -> list[McpServerStatus]:
        return list(self._status.values())

    def tools(self) -> list[BaseTool]:
        """Every server's tools (a stopped server keeps its tools: calling one restarts it)."""
        return [tool for held in self._held.values() for tool in held.tools]

    def gated(self, tool_name: str) -> bool:
        """An MCP tool whose server is not trusted: it needs approval like any side effect."""
        return tool_name.startswith("mcp__") and not any(
            tool_name.startswith(prefix) for prefix in self._trusted
        )

    def _fail(self, name: str, error: str) -> None:
        self._status[name].state, self._status[name].error = "failed", error
        _LOG.warning("MCP server %r unavailable: %s", name, error)

    async def start(self, on_change: Callable[[list[BaseTool]], None]) -> None:
        """Start every usable server at once, then hand ``on_change`` the tools that came up.

        ``on_change`` gets every server's tools again whenever one server's list changes (a
        restart, or a ``notifications/tools/list_changed``).
        """
        await asyncio.gather(*(self._start_one(held) for held in self._held.values()))
        self._on_change = on_change
        on_change(self.tools())

    async def _start_one(self, held: _Held) -> None:
        server = held.config
        self._status[server.name].state, self._status[server.name].error = "starting", None
        held.stopping = False
        held.wake.clear()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        held.task = asyncio.create_task(self._hold(held, ready))
        try:
            await asyncio.wait_for(asyncio.shield(ready), server.start_timeout)
        except TimeoutError:
            held.task.cancel()
            self._fail(server.name, f"did not start within {server.start_timeout:g}s")
            return
        except Exception as exc:
            self._fail(server.name, _reason(exc))
            return
        self._status[server.name].state = "ready"

    async def _list(self, held: _Held, client: Any) -> None:
        listed = await client.list_tools(cache_mode="bypass")
        held.tools = [_wrap(self._call, held.config.name, t) for t in listed.tools]
        self._status[held.config.name].tools = len(held.tools)

    def _changed(self) -> None:
        if self._on_change is not None:  # None during the first start: start() reports once
            self._on_change(self.tools())

    async def _hold(self, held: _Held, ready: asyncio.Future[None]) -> None:
        """Open the server, publish its tools, re-list them on change, keep it open until stopped."""
        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client
        from mcp.types import ToolListChangedNotification

        server = held.config

        async def on_message(message: Any) -> None:
            if isinstance(message, ToolListChangedNotification):
                held.wake.set()  # the holder re-lists: a request from here would block the reader

        try:
            with contextlib.ExitStack() as files:
                if server.url:
                    transport: Any = streamable_http_client(
                        server.url, http_client=create_mcp_http_client(dict(server.headers))
                    )
                else:
                    self._log_dir.mkdir(parents=True, exist_ok=True)
                    errlog = files.enter_context(
                        (self._log_dir / f"mcp-{_UNSAFE.sub('_', server.name)}.log").open("a")
                    )
                    params = StdioServerParameters(
                        command=server.command or "", args=list(server.args), env=dict(server.env)
                    )
                    transport = stdio_client(params, errlog=errlog)
                async with Client(transport, mode="legacy", message_handler=on_message) as client:
                    await self._list(held, client)
                    held.client = client
                    ready.set_result(None)
                    self._changed()  # a restart: its tools may differ
                    while True:
                        await held.wake.wait()
                        held.wake.clear()
                        if held.stopping:
                            break
                        await self._list(held, client)
                        self._changed()
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                _LOG.warning("MCP server %r stopped: %s", server.name, _reason(exc))
        finally:
            held.client = None
            if self._status[server.name].state == "ready":
                self._status[server.name].state = "stopped"

    async def _call(self, name: str, tool: str, arguments: dict[str, Any]) -> Any:
        """Call one tool of a server, restarting the server first when it is down."""
        from mcp.shared.exceptions import MCPError
        from mcp.types import CONNECTION_CLOSED

        held = self._held[name]
        async with held.lock:
            if held.client is None:
                if held.restarts >= MAX_RESTARTS:
                    raise RuntimeError(
                        f"MCP server {name!r} stopped and was restarted {MAX_RESTARTS} times; "
                        "it is not restarted again"
                    )
                held.restarts += 1
                await self._stop_one(held)  # a holder left from a lost connection
                await self._start_one(held)
                if held.client is None:
                    raise RuntimeError(
                        f"MCP server {name!r} could not be restarted: {self._status[name].error}"
                    )
            client = held.client
        try:
            return await client.call_tool(tool, arguments)
        except MCPError as exc:
            if exc.code != CONNECTION_CLOSED:
                raise
            await self._stop_one(held)
            raise RuntimeError(
                f"MCP server {name!r} stopped ({exc}); it is restarted on the next call"
            ) from exc

    async def _stop_one(self, held: _Held) -> None:
        held.stopping, held.client = True, None
        held.wake.set()
        if held.task is not None:
            _done, pending = await asyncio.wait([held.task], timeout=10)
            for task in pending:
                task.cancel()

    async def close(self) -> None:
        """Stop every server (each holder leaves its client, which ends the process)."""
        await asyncio.gather(*(self._stop_one(held) for held in self._held.values()))


def _reason(exc: BaseException) -> str:
    """The innermost useful message of an exception (the SDK wraps errors in groups)."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
