"""The persistent-memory data model (P21)."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["MemoryItem"]


@dataclass(frozen=True)
class MemoryItem:
    """One remembered fact: a short ``key`` and its ``value``, inside a namespace."""

    namespace: str
    key: str
    value: str
    created_at: str
    updated_at: str
