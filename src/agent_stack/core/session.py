from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from langchain_core.messages import BaseMessage

from agent_stack.core.errors import SessionVersionConflictError


@dataclass
class Session:
    """Durable conversation identity and metadata.

    A ``Session`` owns the conversation *identity*, not the execution state. It is a
    persistent object, so it is mutable; callers persist changes via ``SessionStore.save``.
    """

    id: str
    owner_id: str
    agent_name: str
    title: str | None = None
    status: str = "active"
    version: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    # Set on a branch (P30): the session it was forked from and the event it was forked at.
    branch_of: str | None = None
    branch_point_event_id: str | None = None


class SessionStore(Protocol):
    """Infrastructure boundary for session metadata and conversation messages.

    ``save`` supports optimistic concurrency: pass ``expected_version`` (the version a
    caller read via ``get``) to make the write fail with
    :class:`SessionVersionConflictError` when a concurrent writer bumped the stored
    version first. ``expected_version=None`` skips the check and writes unconditionally.
    """

    async def get(self, session_id: str) -> Session | None: ...

    async def save(self, session: Session, *, expected_version: int | None = None) -> None: ...

    async def list_for_owner(self, owner_id: str, limit: int = 50) -> list[Session]:
        """The owner's sessions, most recently active first (at most ``limit``)."""
        ...

    async def get_messages(self, session_id: str) -> list[BaseMessage]: ...

    async def append_messages(self, session_id: str, messages: Sequence[BaseMessage]) -> None: ...

    async def delete(self, session_id: str) -> None: ...


class BranchingSessionStore(SessionStore, Protocol):
    """A store that can fork a conversation at a past event (P30)."""

    async def branch(
        self,
        session_id: str,
        branch_point_event_id: str,
        *,
        agent_name: str | None = None,
    ) -> Session:
        """Create a new session holding ``session_id``'s messages as of that event.

        The branch is independent from then on: neither side writes into the other.
        """
        ...


class InMemorySessionStore:
    """Volatile ``SessionStore`` used by tests and the first composition root.

    Reads return copies so callers can never mutate the store's internal state in place.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._messages: dict[str, list[BaseMessage]] = {}

    async def get(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        return replace(session, metadata=dict(session.metadata))

    async def save(self, session: Session, *, expected_version: int | None = None) -> None:
        stored = self._sessions.get(session.id)
        if expected_version is not None and (stored is None or stored.version != expected_version):
            raise SessionVersionConflictError(
                f"session {session.id} version mismatch: expected "
                f"{expected_version}, stored "
                f"{stored.version if stored is not None else 'absent'}"
            )
        self._sessions[session.id] = replace(session, metadata=dict(session.metadata))

    async def list_for_owner(self, owner_id: str, limit: int = 50) -> list[Session]:
        # Dicts keep insertion order and `save` rewrites in place, so recency here is
        # creation order; a volatile test double does not track activity times.
        owned = [s for s in reversed(self._sessions.values()) if s.owner_id == owner_id]
        return [replace(s, metadata=dict(s.metadata)) for s in owned[:limit]]

    async def get_messages(self, session_id: str) -> list[BaseMessage]:
        return list(self._messages.get(session_id, []))

    async def append_messages(self, session_id: str, messages: Sequence[BaseMessage]) -> None:
        self._messages.setdefault(session_id, []).extend(messages)

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._messages.pop(session_id, None)
