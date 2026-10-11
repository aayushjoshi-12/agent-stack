"""File-based memory: the ``MEMORY.md`` index and its topic files (P16d).

A memory root holds ``MEMORY.md`` (an index: one short line per topic) and a ``topics/``
directory of full topic files. Only the index is resident in the system prompt; topic
bodies are read and written on demand through the ``read_memory_topic`` and
``write_memory_topic`` tools.
"""

from agent_stack.memory.files import MAX_INDEX_BYTES, MemoryFilesError, MemoryIndex
from agent_stack.memory.tools import memory_tools

__all__ = ["MAX_INDEX_BYTES", "MemoryFilesError", "MemoryIndex", "memory_tools"]
