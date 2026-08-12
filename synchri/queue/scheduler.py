"""Deterministic turn scheduling.

Ordering is a total order over ``(priority, enqueue_seq)``:

    0  HUMAN           humans never actually queue; they interrupt
    1  DIRECT_ADDRESS  someone was explicitly addressed
    2  HANDOFF         a model deferred to another model
    3  QUEUED          ordinary requested floor
    4  OPTIONAL        has not spoken yet, may speak if nothing better pending

``enqueue_seq`` is the room's monotonic counter, so ties inside a bucket are
resolved first-come-first-served with no wall-clock dependence.

Every function here assumes the caller already holds the room's write
transaction.  Scheduling decisions are therefore atomic with respect to other
CLI processes: two agents cannot both be promoted to active speaker.
"""

from __future__ import annotations

import sqlite3

from ..ids import new_id, utc_now
from ..models.entities import QueueEntry, Room, Turn
from ..models.enums import Priority, QueueStatus, RoomStatus, TurnKind, TurnStatus
from ..storage import dao


def enqueue(
    conn: sqlite3.Connection,
    room_id: str,
    participant_id: str,
    priority: Priority,
    *,
    seq: int,
    blocking: bool = False,
    reason: str | None = None,
    task_id: str | None = None,
    source_message_id: str | None = None,
) -> QueueEntry:
    """Add or promote a participant in the queue.

    An existing waiting entry is *promoted* when the new priority is stronger,
    which also refreshes its position within the new bucket — being addressed
    directly is a fresh ask, not a resumption of an older one.  A weaker or
    equal priority leaves the existing entry untouched so that FIFO fairness
    inside a bucket is preserved.
    """
    existing = dao.get_live_queue_entry(conn, room_id, participant_id)
    if existing is not None:
        if existing.status == QueueStatus.ACTIVE.value:
            # Already holding the floor; nothing to reorder.
            return existing
        if int(priority) < existing.priority:
            dao.update_queue_entry(
                conn,
                room_id,
                existing.entry_id,
                priority=int(priority),
                enqueue_seq=seq,
                blocking=int(blocking),
                reason=reason,
                task_id=task_id,
                source_message_id=source_message_id,
            )
        elif blocking and not existing.blocking:
            # Same bucket but now a blocking ask: keep position, raise the flag.
            dao.update_queue_entry(
                conn,
                room_id,
                existing.entry_id,
                blocking=1,
                reason=reason,
                task_id=task_id or existing.task_id,
                source_message_id=source_message_id or existing.source_message_id,
            )
        refreshed = dao.get_live_queue_entry(conn, room_id, participant_id)
        assert refreshed is not None  # same transaction, row cannot vanish
        return refreshed

    entry_id = dao.insert_queue_entry(
        conn,
        room_id=room_id,
        participant_id=participant_id,
        priority=int(priority),
        enqueue_seq=seq,
        status=QueueStatus.WAITING.value,
        blocking=int(blocking),
        reason=reason,
        task_id=task_id,
        source_message_id=source_message_id,
        enqueued_at=utc_now(),
    )
    entry = dao.get_live_queue_entry(conn, room_id, participant_id)
    assert entry is not None and entry.entry_id == entry_id
    return entry


def peek(conn: sqlite3.Connection, room_id: str) -> QueueEntry | None:
    """Head of the waiting queue, or ``None`` if nobody is waiting."""
    waiting = dao.list_queue(conn, room_id, statuses=(QueueStatus.WAITING.value,))
    return waiting[0] if waiting else None


def activate_next(conn: sqlite3.Connection, room_id: str) -> Turn | None:
    """Promote the head of the queue to active speaker, if the room permits it.

    No-ops when the room is not active, when a turn is already in flight, or
    when the autonomous-turn budget is exhausted and the room is waiting on its
    human.  Returns the newly started turn, if any.
    """
    room = dao.get_room(conn, room_id)
    if room is None or room.status != RoomStatus.ACTIVE.value:
        return None
    if room.awaiting_human:
        return None
    if room.active_turn_id is not None:
        return None

    entry = peek(conn, room_id)
    if entry is None:
        return None

    turn = start_turn(
        conn,
        room,
        participant_id=entry.participant_id,
        blocking=entry.blocking,
        kind=TurnKind.TARGETED_BLOCKING if entry.blocking else TurnKind.NORMAL,
        task_id=entry.task_id,
        request_message_id=entry.source_message_id,
    )
    dao.update_queue_entry(conn, room_id, entry.entry_id, status=QueueStatus.ACTIVE.value)
    return turn


def start_turn(
    conn: sqlite3.Connection,
    room: Room,
    *,
    participant_id: str,
    blocking: bool,
    kind: TurnKind,
    task_id: str | None = None,
    request_message_id: str | None = None,
) -> Turn:
    """Create an active turn and point the room at it."""
    turn_id = new_id("turn")
    dao.insert_turn(
        conn,
        turn_id=turn_id,
        room_id=room.room_id,
        participant_id=participant_id,
        kind=kind.value,
        status=TurnStatus.ACTIVE.value,
        blocking=int(blocking),
        started_at=utc_now(),
        task_id=task_id,
        request_message_id=request_message_id,
    )
    dao.update_room(
        conn,
        room.room_id,
        active_participant_id=participant_id,
        active_turn_id=turn_id,
    )
    turn = dao.get_turn(conn, room.room_id, turn_id)
    assert turn is not None
    return turn


def end_turn(
    conn: sqlite3.Connection,
    room_id: str,
    turn_id: str,
    status: TurnStatus,
    *,
    response_message_id: str | None = None,
    handoff_target: str | None = None,
    queue_status: QueueStatus = QueueStatus.DONE,
) -> None:
    """Close out a turn and release the floor."""
    turn = dao.get_turn(conn, room_id, turn_id)
    if turn is None:
        return
    dao.update_turn(
        conn,
        room_id,
        turn_id,
        status=status.value,
        ended_at=utc_now(),
        response_message_id=response_message_id,
        handoff_target=handoff_target,
    )
    dao.resolve_queue_entries(
        conn,
        room_id,
        queue_status.value,
        participant_id=turn.participant_id,
        only_statuses=(QueueStatus.ACTIVE.value,),
    )
    room = dao.get_room(conn, room_id)
    if room is not None and room.active_turn_id == turn_id:
        dao.update_room(conn, room_id, active_participant_id=None, active_turn_id=None)


def clear_queue(
    conn: sqlite3.Connection, room_id: str, status: QueueStatus = QueueStatus.CANCELLED
) -> int:
    """Drop every waiting entry (used on human redirect and on room stop)."""
    return dao.resolve_queue_entries(
        conn, room_id, status.value, only_statuses=(QueueStatus.WAITING.value,)
    )


def dequeue(conn: sqlite3.Connection, room_id: str, participant_id: str, status: QueueStatus) -> None:
    """Remove one participant's live entries from the queue."""
    dao.resolve_queue_entries(
        conn,
        room_id,
        status.value,
        participant_id=participant_id,
        only_statuses=(QueueStatus.WAITING.value, QueueStatus.ACTIVE.value),
    )


def record_agent_turn(conn: sqlite3.Connection, room_id: str) -> bool:
    """Count one autonomous agent turn against the room budget.

    Returns ``True`` when the budget is now exhausted, meaning the room has
    flipped to ``awaiting_human`` and agents must stop until a human speaks.
    This is the loop breaker: without it, two agents that keep addressing each
    other would never yield the room back.
    """
    room = dao.get_room(conn, room_id)
    if room is None:
        return False
    count = room.consecutive_agent_turns + 1
    exhausted = count >= room.max_consecutive_agent_turns
    dao.update_room(
        conn,
        room_id,
        consecutive_agent_turns=count,
        awaiting_human=int(exhausted),
    )
    return exhausted


def reset_agent_turn_budget(conn: sqlite3.Connection, room_id: str) -> None:
    """A human spoke: the room is free to run autonomously again."""
    dao.update_room(conn, room_id, consecutive_agent_turns=0, awaiting_human=0)


def queue_snapshot(conn: sqlite3.Connection, room_id: str) -> list[dict]:
    """Ordered view of who speaks next and why (for CLI and future UI)."""
    entries = dao.list_queue(conn, room_id, statuses=(QueueStatus.WAITING.value,))
    return [
        {
            "position": index + 1,
            "participant": entry.participant_name,
            "participant_id": entry.participant_id,
            "priority": entry.priority,
            "priority_name": Priority(entry.priority).name.lower(),
            "blocking": entry.blocking,
            "reason": entry.reason,
            "task_id": entry.task_id,
            "source_message_id": entry.source_message_id,
        }
        for index, entry in enumerate(entries)
    ]
