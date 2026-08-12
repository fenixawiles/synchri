"""Closed vocabularies used across the orchestration state."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """``str`` subclass enum so values round-trip through SQLite and JSON."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return str(self.value)


class RoomStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    STOPPED = "stopped"


class ParticipantKind(StrEnum):
    AGENT = "agent"
    HUMAN = "human"


class ParticipantStatus(StrEnum):
    ACTIVE = "active"
    REMOVED = "removed"


class InviteStatus(StrEnum):
    PENDING = "pending"
    REDEEMED = "redeemed"
    REVOKED = "revoked"
    EXPIRED = "expired"


class MessageType(StrEnum):
    CHAT = "chat"
    TASK = "task"
    RESPONSE = "response"
    PASS = "pass"
    HANDOFF = "handoff"
    INTERRUPT = "interrupt"
    SYSTEM = "system"


class ResponseStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    DECLINED = "declined"
    FAILED = "failed"


class TurnKind(StrEnum):
    TARGETED_BLOCKING = "targeted_blocking"
    NORMAL = "normal"
    HUMAN = "human"


class TurnStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    PASSED = "passed"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"


class QueueStatus(StrEnum):
    WAITING = "waiting"
    ACTIVE = "active"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    PASSED = "passed"
    CANCELLED = "cancelled"


class Priority(int, Enum):
    """Scheduling buckets, lowest number wins.

    Mirrors the documented priority order.  ``HUMAN`` exists for completeness:
    a human never actually queues, because human messages bypass the queue and
    interrupt whatever holds the floor.
    """

    HUMAN = 0
    DIRECT_ADDRESS = 1
    HANDOFF = 2
    QUEUED = 3
    OPTIONAL = 4

    @classmethod
    def from_name(cls, name: str) -> "Priority":
        try:
            return cls[name.strip().upper()]
        except KeyError as exc:  # pragma: no cover - guarded at CLI layer
            raise ValueError(f"unknown priority: {name!r}") from exc


class TurnState(StrEnum):
    """What ``wait``/``turn`` reports back to a polling participant."""

    YOUR_TURN = "your_turn"
    WAITING = "waiting"
    BLOCKED = "blocked"
    IDLE = "idle"
    AWAITING_HUMAN = "awaiting_human"
    PAUSED = "paused"
    STOPPED = "stopped"
    REMOVED = "removed"
