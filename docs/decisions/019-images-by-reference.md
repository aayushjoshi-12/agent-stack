# 019 Images: one block shape, stored by reference

Status: accepted (P44)

## Context
Users attach images (TUI, web, Telegram) and tools return them (`read_file`, `web_screenshot`,
MCP). The model must see them on the turn and on every later turn, including after a restart.
Conversations are files (ADR 008): `transcript.jsonl` and `transcript.md` should stay small,
readable and greppable, and the event log, audit log, export and search index must never fill up
with base64.

## Decision
- **One shape in memory**: the OpenAI-style `image_url` block with a base64 data URL, for user
  messages and tool results. Verified by real calls through `ChatOpenRouter`: it converts
  LangChain's v1 `image` blocks only inside a HumanMessage and sends ToolMessage content as it is,
  so a v1 block in a tool result is not seen (Claude Haiku: "can't see the image"; Gemini guessed
  wrong). `image_url` works in both places on both, and langchain-anthropic and langchain-ollama
  accept it too. Images go before the text.
- **Stored by reference, content addressed**: on append, the file conversation store writes each
  image once to `artifacts/<sha256[:16]>.<ext>` and the transcript line keeps the text in
  `content` plus `data: {"images": [names]}`. On read the names become image blocks again (the
  same blocks as the first turn). The name is derived from the bytes, so display code can name any
  in-memory block without extra metadata, and a resent image is one file.
- **Placeholders everywhere a person reads**: `[image: <name>]` in `transcript.md`, the export,
  the chat search index and history rows. Events never carry message content, so the event and
  audit logs need nothing.
- **Limits**: PNG, JPEG, WebP, GIF, sniffed from the bytes; 5 MB per image (Anthropic's cap).
  No downscaling: it would need Pillow, and the providers already downscale.
- **No vision**: one retry of that call with images as text placeholders that say the model does
  not accept images and to switch with `/model`. The stored conversation keeps the images, so a
  later switch to a vision model still sees them; a text-only model is never stuck.

## Consequences
- The original file name is not stored: history after a reload shows the hash name; the live
  TUI row and web bubble show the original name or the image itself.
- `message_to_record` stays pure (it only names images); writing and reading the files lives in
  `FileConversationStore`. A store that keeps messages in memory keeps the blocks as they are.
- Queued input (`send_input`) stays text only.
- Each later turn re-sends the image bytes to the provider (as with any multimodal history); the
  token estimate counts 1600 tokens per image, and compaction or the window drop old ones.
