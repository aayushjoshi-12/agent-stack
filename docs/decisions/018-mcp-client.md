# 018 MCP client: config shape, library, lifecycle, approval

Status: accepted (P43)

## Context
Tools from MCP servers should be tools of the agent, configured the way people already
configure them for other agents, without slowing startup or weakening the approval flow.

## Decision
- **Config** is the Claude Code shape, `{"mcpServers": {name: {"command", "args", "env"} |
  {"url", "headers"}}}`, read from `~/.agent-stack/mcp.json` then `<workspace>/.mcp.json`; the
  workspace wins per server name. `${VAR}` in command, args, env, url and headers is expanded
  from the environment (unset: empty). Optional `"trusted": true` per server. A bad entry or file
  is logged and reported, never fatal. The environment is read by bootstrap and passed in
  (only the listed modules read `os.environ`).
- **Library**: the official `mcp` SDK (2.x) as a regular dependency, not `langchain-mcp-adapters`.
  `mcp.Client` already covers stdio and streamable HTTP; the adapter would be a second
  dependency for ~30 lines (wrap each tool in a `StructuredTool` with its JSON schema, name it,
  turn the result into text). Its dependencies mostly overlap fastapi/uvicorn/httpx.
- **Names**: `mcp__<server>__<tool>` (unsafe characters become `_`, capped at 64).
  Results: text blocks joined; other blocks named by type; `is_error` raises, so the executor
  returns an error ToolMessage. The per-call tool timeout (P35) applies unchanged.
- **Lifecycle**: `build_application` only reads the config (no process, no network). Servers
  start on the first message (or `/mcp`), all at once, each bounded by 30 s, each held open by
  its own task (the SDK's scopes must be entered and left in one task). Their tools join the main
  agent by rebuilding its runtime, the same path as `/model`; subagents do not get them.
  `drain_background()` closes them; the event loop's teardown cancels the holders otherwise,
  which ends the server processes. stdio servers' stderr goes to `.agent/logs/mcp-<name>.log`.
- **Approval**: MCP tools are side-effecting by default: `ToolPolicy.dynamic` gates every
  `mcp__*` tool of an untrusted server. In ask mode the user decides; in auto mode no rule
  matches them, so the classifier decides (never auto-allowed by rules). `"trusted": true` skips
  approval for that server's tools.

## Consequences
- The first message after start waits for the servers (up to 30 s for a slow one); the UI opens
  immediately. A server that dies later turns its tool calls into error results.
- No reconnect, no tool-list refresh, no resources/prompts: add when needed.
