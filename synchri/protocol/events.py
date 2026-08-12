"""Event type vocabulary for the audit log.

State transitions that are not themselves messages are recorded as events so
that provenance covers the whole room history, not just the chat.  Event names
are part of the wire contract: a future UI or adapter can subscribe to them.
"""

from __future__ import annotations

ROOM_CREATED = "room.created"
ROOM_PAUSED = "room.paused"
ROOM_RESUMED = "room.resumed"
ROOM_STOPPED = "room.stopped"
ROOM_CONFIG_UPDATED = "room.config_updated"

INVITE_CREATED = "invite.created"
INVITE_REVOKED = "invite.revoked"

PARTICIPANT_JOINED = "participant.joined"
PARTICIPANT_REJOINED = "participant.rejoined"
PARTICIPANT_REMOVED = "participant.removed"

MESSAGE_SENT = "message.sent"
MESSAGE_PASSED = "message.passed"

TURN_STARTED = "turn.started"
TURN_COMPLETED = "turn.completed"
TURN_PASSED = "turn.passed"
TURN_INTERRUPTED = "turn.interrupted"

QUEUE_PROMOTED = "queue.promoted"
QUEUE_CLEARED = "queue.cleared"
QUEUE_FLOOR_REQUESTED = "queue.floor_requested"

HANDOFF_RECORDED = "handoff.recorded"
TASK_OPENED = "task.opened"
TASK_CLOSED = "task.closed"

HUMAN_INTERRUPT = "human.interrupt"
AUTONOMY_LIMIT_REACHED = "autonomy.limit_reached"

MEMORY_UPDATED = "memory.updated"

ALL = [value for name, value in list(globals().items()) if name.isupper() and name != "ALL"]
