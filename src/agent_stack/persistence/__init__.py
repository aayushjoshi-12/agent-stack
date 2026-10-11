"""Persistence: the file-based conversation transcript plus the SQLite index.

- :mod:`agent_stack.persistence.conversation` — the full conversation as per-session files.
- :mod:`agent_stack.persistence.sqlite` — session/run metadata and index rows.
- :mod:`agent_stack.persistence.serializers` — LangChain message ⇄ JSONL records.
- :mod:`agent_stack.persistence.migrations` — schema bootstrap.
- :mod:`agent_stack.persistence.memory` — volatile in-memory implementations.
"""
