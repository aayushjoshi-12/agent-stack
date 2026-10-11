"""Filesystem tools — implemented in Phase P13.

Placeholders only: they exist so the package is importable and typed while the
workspace-sandbox (path-escape protection, size limits) is built in P13. Do not wire
these into an agent until P13.
"""

from __future__ import annotations


async def ls(path: str) -> str:
    raise NotImplementedError("filesystem.ls lands in Phase P13")


async def read(path: str) -> str:
    raise NotImplementedError("filesystem.read lands in Phase P13")


async def write(path: str, content: str) -> str:
    raise NotImplementedError("filesystem.write lands in Phase P13")


async def edit(path: str, old: str, new: str) -> str:
    raise NotImplementedError("filesystem.edit lands in Phase P13")
