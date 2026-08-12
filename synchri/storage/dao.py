"""Row-level data access.

Every function here takes an explicit ``room_id`` and filters on it.  That is
the enforcement point for cross-room isolation invariant: there is no query in
this module that can return rows belonging to a room the caller did not name.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Sequence

from ..ids import new_id, utc_now
from ..models.entities import Event, Invite, Participant, QueueEntry, Room, Task, Turn
from ..models.enums import ParticipantStatus, TaskStatus, TurnStatus
from ..models.envelope import MessageEnvelope

# --------------------------------------------------------------------------
# sequence
# --------------------------------------------------------------------------


def next_seq(conn: sqlite3.Connection, room_id: str) -> int:
    """Bump and return the room's monotonic sequence counter.

    Callers must already hold the write transaction; the UPDATE...RETURNING is
    atomic within it, so two concurrent processes cannot mint the same seq.
    """
    row = conn.execute(
        "UPDATE rooms SET seq = seq + 1, updated_at = ? WHERE room_id = ? RETURNING seq",
        (utc_now(), room_id),
    ).fetchone()
    if row is None:
        raise KeyError(room_id)
    return int(row["seq"])


# --------------------------------------------------------------------------
# rooms
# --------------------------------------------------------------------------


def insert_room(conn: sqlite3.Connection, **fields) -> None:
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO rooms ({columns}) VALUES ({placeholders})", tuple(fields.values()))


def get_room(conn: sqlite3.Connection, room_id: str) -> Room | None:
    row = conn.execute("SELECT * FROM rooms WHERE room_id = ?", (room_id,)).fetchone()
    return Room.from_row(row) if row else None


def get_room_token_hash(conn: sqlite3.Connection, room_id: str) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT token_salt, token_hash FROM rooms WHERE room_id = ?", (room_id,)
    ).fetchone()
    return (row["token_salt"], row["token_hash"]) if row else None


def list_rooms(conn: sqlite3.Connection) -> list[Room]:
    rows = conn.execute("SELECT * FROM rooms ORDER BY created_at DESC").fetchall()
    return [Room.from_row(r) for r in rows]


def list_rooms_for_workspace(
    conn: sqlite3.Connection, workspace_root: str, status: str | None = None
) -> list[Room]:
    """Rooms bound to a working tree, newest first."""
    sql = "SELECT * FROM rooms WHERE workspace_root = ?"
    params: list[object] = [workspace_root]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at DESC"
    return [Room.from_row(r) for r in conn.execute(sql, params).fetchall()]


def last_message_seq_by(conn: sqlite3.Connection, room_id: str, participant_id: str) -> int:
    """Seq of this participant's most recent message, or 0 if it never spoke."""
    row = conn.execute(
        "SELECT MAX(seq) AS seq FROM messages WHERE room_id = ? AND sender_participant_id = ?",
        (room_id, participant_id),
    ).fetchone()
    return int(row["seq"] or 0)


def update_room(conn: sqlite3.Connection, room_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = utc_now()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE rooms SET {assignments} WHERE room_id = ?",
        (*fields.values(), room_id),
    )


# --------------------------------------------------------------------------
# participants
# --------------------------------------------------------------------------


def insert_participant(conn: sqlite3.Connection, **fields) -> None:
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO participants ({columns}) VALUES ({placeholders})", tuple(fields.values())
    )


def get_participant(conn: sqlite3.Connection, room_id: str, participant_id: str) -> Participant | None:
    row = conn.execute(
        "SELECT * FROM participants WHERE room_id = ? AND participant_id = ?",
        (room_id, participant_id),
    ).fetchone()
    return Participant.from_row(row) if row else None


def get_participant_by_name(conn: sqlite3.Connection, room_id: str, name: str) -> Participant | None:
    row = conn.execute(
        "SELECT * FROM participants WHERE room_id = ? AND name = ?", (room_id, name)
    ).fetchone()
    return Participant.from_row(row) if row else None


def get_participant_secret(
    conn: sqlite3.Connection, room_id: str, participant_id: str
) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT secret_salt, secret_hash FROM participants WHERE room_id = ? AND participant_id = ?",
        (room_id, participant_id),
    ).fetchone()
    return (row["secret_salt"], row["secret_hash"]) if row else None


def list_participants(
    conn: sqlite3.Connection, room_id: str, include_removed: bool = True
) -> list[Participant]:
    sql = "SELECT * FROM participants WHERE room_id = ?"
    params: list[object] = [room_id]
    if not include_removed:
        sql += " AND status = ?"
        params.append(ParticipantStatus.ACTIVE.value)
    # Several participants can be created within the same millisecond. rowid
    # preserves their insertion order as the deterministic tie-breaker.
    sql += " ORDER BY joined_at ASC, rowid ASC"
    return [Participant.from_row(r) for r in conn.execute(sql, params).fetchall()]


def update_participant(conn: sqlite3.Connection, room_id: str, participant_id: str, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE participants SET {assignments} WHERE room_id = ? AND participant_id = ?",
        (*fields.values(), room_id, participant_id),
    )


def touch_participant(conn: sqlite3.Connection, room_id: str, participant_id: str) -> None:
    conn.execute(
        "UPDATE participants SET last_seen_at = ? WHERE room_id = ? AND participant_id = ?",
        (utc_now(), room_id, participant_id),
    )


# --------------------------------------------------------------------------
# transient agent activity
# --------------------------------------------------------------------------


def upsert_activity(
    conn: sqlite3.Connection,
    room_id: str,
    participant_id: str,
    summary: str,
    *,
    expires_at: str,
) -> dict:
    """Record a short public work note without creating a room message."""
    updated_at = utc_now()
    conn.execute(
        "INSERT INTO agent_activity (room_id, participant_id, summary, updated_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(room_id, participant_id) DO UPDATE SET "
        "summary=excluded.summary, updated_at=excluded.updated_at, expires_at=excluded.expires_at",
        (room_id, participant_id, summary, updated_at, expires_at),
    )
    # Keep a short, transient progression visible in the UI while the turn is
    # active. This is not a message and is deleted at the response boundary.
    conn.execute(
        "DELETE FROM agent_activity_entries WHERE room_id = ? AND expires_at <= ?",
        (room_id, updated_at),
    )
    cursor = conn.execute(
        "INSERT INTO agent_activity_entries "
        "(room_id, participant_id, summary, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (room_id, participant_id, summary, updated_at, expires_at),
    )
    conn.execute("UPDATE rooms SET updated_at = ? WHERE room_id = ?", (updated_at, room_id))
    return {
        "entry_id": int(cursor.lastrowid),
        "participant_id": participant_id,
        "summary": summary,
        "created_at": updated_at,
        "updated_at": updated_at,  # current-note compatibility for callers
        "expires_at": expires_at,
    }


def clear_activity(
    conn: sqlite3.Connection, room_id: str, participant_id: str | None = None
) -> int:
    """Remove one participant's current work note, or all notes in a room."""
    sql = "DELETE FROM agent_activity WHERE room_id = ?"
    params: list[object] = [room_id]
    if participant_id is not None:
        sql += " AND participant_id = ?"
        params.append(participant_id)
    removed = int(conn.execute(sql, params).rowcount)
    entry_sql = "DELETE FROM agent_activity_entries WHERE room_id = ?"
    if participant_id is not None:
        entry_sql += " AND participant_id = ?"
    removed += int(conn.execute(entry_sql, params).rowcount)
    if removed:
        conn.execute("UPDATE rooms SET updated_at = ? WHERE room_id = ?", (utc_now(), room_id))
    return removed


def list_live_activities(conn: sqlite3.Connection, room_id: str, *, now: str | None = None) -> list[dict]:
    """Current public work notes, scoped to active participants in this room."""
    rows = conn.execute(
        "SELECT a.participant_id, p.name, a.summary, a.updated_at, a.expires_at "
        "FROM agent_activity a JOIN participants p ON p.participant_id = a.participant_id "
        "WHERE a.room_id = ? AND p.room_id = ? AND p.status = ? AND a.expires_at > ? "
        "ORDER BY a.updated_at ASC, p.name ASC",
        (room_id, room_id, ParticipantStatus.ACTIVE.value, now or utc_now()),
    ).fetchall()
    return [dict(row) for row in rows]


def list_live_activity_entries(
    conn: sqlite3.Connection, room_id: str, *, now: str | None = None
) -> list[dict]:
    """The current turn's public progress trail, never the room transcript."""
    rows = conn.execute(
        "SELECT e.entry_id, e.participant_id, p.name, e.summary, e.created_at, e.expires_at "
        "FROM agent_activity_entries e JOIN participants p ON p.participant_id = e.participant_id "
        "WHERE e.room_id = ? AND p.room_id = ? AND p.status = ? AND e.expires_at > ? "
        "ORDER BY e.created_at ASC, e.entry_id ASC",
        (room_id, room_id, ParticipantStatus.ACTIVE.value, now or utc_now()),
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------
# invites
# --------------------------------------------------------------------------


def insert_invite(conn: sqlite3.Connection, **fields) -> None:
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO invites ({columns}) VALUES ({placeholders})", tuple(fields.values()))


def list_invites(conn: sqlite3.Connection, room_id: str, live_only: bool = False) -> list[Invite]:
    sql = "SELECT * FROM invites WHERE room_id = ?"
    if live_only:
        sql += " AND redeemed_at IS NULL AND revoked_at IS NULL"
    sql += " ORDER BY created_at ASC"
    return [Invite.from_row(r) for r in conn.execute(sql, (room_id,)).fetchall()]


def get_live_invite_by_name(conn: sqlite3.Connection, room_id: str, name: str) -> Invite | None:
    row = conn.execute(
        "SELECT * FROM invites WHERE room_id = ? AND participant_name = ? "
        "AND redeemed_at IS NULL AND revoked_at IS NULL",
        (room_id, name),
    ).fetchone()
    return Invite.from_row(row) if row else None


def get_invite_secret(conn: sqlite3.Connection, room_id: str, invite_id: str) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT token_salt, token_hash FROM invites WHERE room_id = ? AND invite_id = ?",
        (room_id, invite_id),
    ).fetchone()
    return (row["token_salt"], row["token_hash"]) if row else None


def update_invite(conn: sqlite3.Connection, room_id: str, invite_id: str, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE invites SET {assignments} WHERE room_id = ? AND invite_id = ?",
        (*fields.values(), room_id, invite_id),
    )


def revoke_live_invites(conn: sqlite3.Connection, room_id: str, reason: str) -> int:
    return int(
        conn.execute(
            "UPDATE invites SET revoked_at = ?, revoked_reason = ? WHERE room_id = ? "
            "AND redeemed_at IS NULL AND revoked_at IS NULL",
            (utc_now(), reason, room_id),
        ).rowcount
    )


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------


def insert_message(conn: sqlite3.Connection, envelope: MessageEnvelope) -> None:
    row = envelope.to_row()
    columns = ", ".join(row)
    placeholders = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO messages ({columns}) VALUES ({placeholders})", tuple(row.values()))


def get_message(conn: sqlite3.Connection, room_id: str, message_id: str) -> MessageEnvelope | None:
    row = conn.execute(
        "SELECT * FROM messages WHERE room_id = ? AND message_id = ?", (room_id, message_id)
    ).fetchone()
    return MessageEnvelope.from_row(row) if row else None


def list_messages(
    conn: sqlite3.Connection,
    room_id: str,
    since_seq: int = 0,
    limit: int | None = None,
) -> list[MessageEnvelope]:
    sql = "SELECT * FROM messages WHERE room_id = ? AND seq > ? ORDER BY seq ASC"
    params: list[object] = [room_id, since_seq]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [MessageEnvelope.from_row(r) for r in conn.execute(sql, params).fetchall()]


def list_recent_messages(conn: sqlite3.Connection, room_id: str, limit: int) -> list[MessageEnvelope]:
    rows = conn.execute(
        "SELECT * FROM (SELECT * FROM messages WHERE room_id = ? ORDER BY seq DESC LIMIT ?) "
        "ORDER BY seq ASC",
        (room_id, int(limit)),
    ).fetchall()
    return [MessageEnvelope.from_row(r) for r in rows]


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------

_QUEUE_SELECT = """
    SELECT q.*, p.name AS participant_name
      FROM queue_entries q
      JOIN participants p ON p.participant_id = q.participant_id
"""


def get_live_queue_entry(
    conn: sqlite3.Connection, room_id: str, participant_id: str
) -> QueueEntry | None:
    row = conn.execute(
        _QUEUE_SELECT + " WHERE q.room_id = ? AND q.participant_id = ? AND q.status IN ('waiting','active')",
        (room_id, participant_id),
    ).fetchone()
    return QueueEntry.from_row(row) if row else None


def list_queue(conn: sqlite3.Connection, room_id: str, statuses: Sequence[str] = ("waiting",)) -> list[QueueEntry]:
    placeholders = ", ".join("?" for _ in statuses)
    rows = conn.execute(
        _QUEUE_SELECT
        + f" WHERE q.room_id = ? AND q.status IN ({placeholders})"
        + " ORDER BY q.priority ASC, q.enqueue_seq ASC",
        (room_id, *statuses),
    ).fetchall()
    return [QueueEntry.from_row(r) for r in rows]


def insert_queue_entry(conn: sqlite3.Connection, **fields) -> int:
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    cursor = conn.execute(
        f"INSERT INTO queue_entries ({columns}) VALUES ({placeholders})", tuple(fields.values())
    )
    return int(cursor.lastrowid)


def update_queue_entry(conn: sqlite3.Connection, room_id: str, entry_id: int, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE queue_entries SET {assignments} WHERE room_id = ? AND entry_id = ?",
        (*fields.values(), room_id, entry_id),
    )


def resolve_queue_entries(
    conn: sqlite3.Connection,
    room_id: str,
    status: str,
    *,
    participant_id: str | None = None,
    only_statuses: Iterable[str] = ("waiting", "active"),
) -> int:
    placeholders = ", ".join("?" for _ in only_statuses)
    sql = (
        f"UPDATE queue_entries SET status = ?, resolved_at = ? "
        f"WHERE room_id = ? AND status IN ({placeholders})"
    )
    params: list[object] = [status, utc_now(), room_id, *only_statuses]
    if participant_id is not None:
        sql += " AND participant_id = ?"
        params.append(participant_id)
    return int(conn.execute(sql, params).rowcount)


# --------------------------------------------------------------------------
# turns
# --------------------------------------------------------------------------

_TURN_SELECT = """
    SELECT t.*, p.name AS participant_name
      FROM turns t
      JOIN participants p ON p.participant_id = t.participant_id
"""


def insert_turn(conn: sqlite3.Connection, **fields) -> None:
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO turns ({columns}) VALUES ({placeholders})", tuple(fields.values()))


def get_turn(conn: sqlite3.Connection, room_id: str, turn_id: str) -> Turn | None:
    row = conn.execute(
        _TURN_SELECT + " WHERE t.room_id = ? AND t.turn_id = ?", (room_id, turn_id)
    ).fetchone()
    return Turn.from_row(row) if row else None


def get_active_turn(conn: sqlite3.Connection, room_id: str) -> Turn | None:
    row = conn.execute(
        _TURN_SELECT + " WHERE t.room_id = ? AND t.status = ? ORDER BY t.started_at DESC LIMIT 1",
        (room_id, TurnStatus.ACTIVE.value),
    ).fetchone()
    return Turn.from_row(row) if row else None


def list_turns(conn: sqlite3.Connection, room_id: str, limit: int = 50) -> list[Turn]:
    rows = conn.execute(
        _TURN_SELECT + " WHERE t.room_id = ? ORDER BY t.started_at DESC LIMIT ?",
        (room_id, int(limit)),
    ).fetchall()
    return [Turn.from_row(r) for r in rows]


def update_turn(conn: sqlite3.Connection, room_id: str, turn_id: str, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE turns SET {assignments} WHERE room_id = ? AND turn_id = ?",
        (*fields.values(), room_id, turn_id),
    )


# --------------------------------------------------------------------------
# tasks
# --------------------------------------------------------------------------


def insert_task(conn: sqlite3.Connection, **fields) -> None:
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(f"INSERT INTO tasks ({columns}) VALUES ({placeholders})", tuple(fields.values()))


def get_task(conn: sqlite3.Connection, room_id: str, task_id: str) -> Task | None:
    row = conn.execute(
        "SELECT * FROM tasks WHERE room_id = ? AND task_id = ?", (room_id, task_id)
    ).fetchone()
    return Task.from_row(row) if row else None


def list_tasks(conn: sqlite3.Connection, room_id: str, status: str | None = None) -> list[Task]:
    sql = "SELECT * FROM tasks WHERE room_id = ?"
    params: list[object] = [room_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY created_at ASC"
    return [Task.from_row(r) for r in conn.execute(sql, params).fetchall()]


def update_task(conn: sqlite3.Connection, room_id: str, task_id: str, **fields) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE tasks SET {assignments} WHERE room_id = ? AND task_id = ?",
        (*fields.values(), room_id, task_id),
    )


def cancel_open_tasks(conn: sqlite3.Connection, room_id: str) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, completed_at = ? WHERE room_id = ? AND status = ?",
        (TaskStatus.CANCELLED.value, utc_now(), room_id, TaskStatus.OPEN.value),
    )


# --------------------------------------------------------------------------
# events
# --------------------------------------------------------------------------


def insert_event(
    conn: sqlite3.Connection,
    room_id: str,
    event_type: str,
    *,
    seq: int,
    actor_participant_id: str | None = None,
    actor_name: str | None = None,
    payload: dict | None = None,
) -> Event:
    event = Event(
        event_id=new_id("evt"),
        room_id=room_id,
        seq=seq,
        event_type=event_type,
        created_at=utc_now(),
        actor_participant_id=actor_participant_id,
        actor_name=actor_name,
        payload=payload or {},
    )
    conn.execute(
        "INSERT INTO events (event_id, room_id, seq, event_type, actor_participant_id, "
        "actor_name, payload, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.event_id,
            event.room_id,
            event.seq,
            event.event_type,
            event.actor_participant_id,
            event.actor_name,
            json.dumps(event.payload, ensure_ascii=False),
            event.created_at,
        ),
    )
    return event


def list_events(
    conn: sqlite3.Connection, room_id: str, since_seq: int = 0, limit: int | None = None
) -> list[Event]:
    sql = "SELECT * FROM events WHERE room_id = ? AND seq > ? ORDER BY seq ASC"
    params: list[object] = [room_id, since_seq]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    return [Event.from_row(r) for r in conn.execute(sql, params).fetchall()]


__all__ = [name for name in dir() if not name.startswith("_")]
