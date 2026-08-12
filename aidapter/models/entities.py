"""Read-model dataclasses for orchestration entities.

These are projections of SQLite rows.  They are never the authoritative copy of
anything: the broker reads them inside a transaction, decides, and writes back.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from .enums import ParticipantKind, ParticipantStatus, RoomStatus


@dataclass
class Room:
    room_id: str
    name: str
    status: str
    created_at: str
    updated_at: str
    seq: int
    max_consecutive_agent_turns: int
    consecutive_agent_turns: int
    awaiting_human: bool
    active_participant_id: str | None = None
    active_turn_id: str | None = None
    owner_participant_id: str | None = None
    stopped_at: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status == RoomStatus.ACTIVE.value

    @property
    def is_stopped(self) -> bool:
        return self.status == RoomStatus.STOPPED.value

    @classmethod
    def from_row(cls, row) -> "Room":
        data = dict(row)
        return cls(
            room_id=data["room_id"],
            name=data["name"],
            status=data["status"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            seq=data["seq"],
            max_consecutive_agent_turns=data["max_consecutive_agent_turns"],
            consecutive_agent_turns=data["consecutive_agent_turns"],
            awaiting_human=bool(data["awaiting_human"]),
            active_participant_id=data["active_participant_id"],
            active_turn_id=data["active_turn_id"],
            owner_participant_id=data["owner_participant_id"],
            stopped_at=data["stopped_at"],
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Participant:
    participant_id: str
    room_id: str
    name: str
    kind: str
    status: str
    joined_at: str
    last_seen_at: str | None = None
    removed_at: str | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_human(self) -> bool:
        return self.kind == ParticipantKind.HUMAN.value

    @property
    def is_active(self) -> bool:
        return self.status == ParticipantStatus.ACTIVE.value

    @classmethod
    def from_row(cls, row) -> "Participant":
        data = dict(row)
        return cls(
            participant_id=data["participant_id"],
            room_id=data["room_id"],
            name=data["name"],
            kind=data["kind"],
            status=data["status"],
            joined_at=data["joined_at"],
            last_seen_at=data["last_seen_at"],
            removed_at=data["removed_at"],
            metadata=json.loads(data["metadata"] or "{}"),
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class QueueEntry:
    entry_id: int
    room_id: str
    participant_id: str
    participant_name: str
    priority: int
    enqueue_seq: int
    status: str
    blocking: bool
    reason: str | None = None
    task_id: str | None = None
    source_message_id: str | None = None
    enqueued_at: str | None = None

    @classmethod
    def from_row(cls, row) -> "QueueEntry":
        data = dict(row)
        return cls(
            entry_id=data["entry_id"],
            room_id=data["room_id"],
            participant_id=data["participant_id"],
            participant_name=data.get("participant_name") or "",
            priority=data["priority"],
            enqueue_seq=data["enqueue_seq"],
            status=data["status"],
            blocking=bool(data["blocking"]),
            reason=data["reason"],
            task_id=data["task_id"],
            source_message_id=data["source_message_id"],
            enqueued_at=data["enqueued_at"],
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Turn:
    turn_id: str
    room_id: str
    participant_id: str
    participant_name: str
    kind: str
    status: str
    started_at: str
    blocking: bool
    task_id: str | None = None
    request_message_id: str | None = None
    response_message_id: str | None = None
    handoff_target: str | None = None
    ended_at: str | None = None

    @classmethod
    def from_row(cls, row) -> "Turn":
        data = dict(row)
        return cls(
            turn_id=data["turn_id"],
            room_id=data["room_id"],
            participant_id=data["participant_id"],
            participant_name=data.get("participant_name") or "",
            kind=data["kind"],
            status=data["status"],
            started_at=data["started_at"],
            blocking=bool(data["blocking"]),
            task_id=data["task_id"],
            request_message_id=data["request_message_id"],
            response_message_id=data["response_message_id"],
            handoff_target=data["handoff_target"],
            ended_at=data["ended_at"],
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Task:
    task_id: str
    room_id: str
    status: str
    goal: str | None
    created_by: str
    assigned_to: str | None
    created_at: str
    request_message_id: str | None = None
    response_message_id: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row) -> "Task":
        data = dict(row)
        return cls(**{k: data[k] for k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Event:
    """An audit record for a state transition (as opposed to a message)."""

    event_id: str
    room_id: str
    seq: int
    event_type: str
    created_at: str
    actor_participant_id: str | None = None
    actor_name: str | None = None
    payload: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row) -> "Event":
        data = dict(row)
        return cls(
            event_id=data["event_id"],
            room_id=data["room_id"],
            seq=data["seq"],
            event_type=data["event_type"],
            created_at=data["created_at"],
            actor_participant_id=data["actor_participant_id"],
            actor_name=data["actor_name"],
            payload=json.loads(data["payload"] or "{}"),
        )

    def to_dict(self) -> dict:
        return asdict(self)
