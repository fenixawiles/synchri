"""Server-sent events: the dashboard stops guessing when to refresh.

Polling from the browser meant a fixed 4-second lag and a request whether or
not anything happened. Instead the server watches — cheaply, in one place — and
pushes a small event the moment something changes.

SSE rather than WebSocket on purpose: it is one long-lived GET, it reconnects on
its own, `EventSource` is three lines in the browser, and it needs no handshake
or frame codec written by hand. The traffic here is strictly server → client;
the client already has a JSON API for everything it sends.

Each stream gets its own :class:`Broker`, and therefore its own SQLite
connection: sharing one across stream threads would interleave reads.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from ..broker import Broker
from ..config import Workspace
from ..session import drafts as drafts_module
from ..session.manager import SessionManager
from ..storage import dao

#: How often the server checks for change. The client never polls; this is the
#: one loop, and it reads a handful of integers.
POLL_SECONDS = 0.75
#: Comment frames keep proxies and browsers from timing the connection out.
HEARTBEAT_SECONDS = 20.0
#: Give up eventually so an abandoned tab cannot hold a thread forever.
MAX_STREAM_SECONDS = 60 * 60 * 6


@dataclass(frozen=True)
class Fingerprint:
    """A cheap summary of everything the UI renders.

    If this is unchanged, nothing the dashboard shows has changed, so no event
    is sent. It is deliberately coarse: the client refetches on any change, and
    correctness comes from the refetch, not from this being precise.
    """

    sessions: tuple
    room_seq: tuple[int, str]
    activities: tuple
    drafts: tuple

    @classmethod
    def take(cls, manager: SessionManager, session_id: str | None) -> "Fingerprint":
        records = manager.list_sessions()
        sessions = tuple(
            (r.session_id, r.status, r.updated_at, r.contract_revision) for r in records
        )
        room_seq = (0, "")
        activities: tuple = ()
        if session_id:
            try:
                record = manager.get(session_id, sweep=False)
            except Exception:  # pragma: no cover - session removed mid-stream
                record = None
            if record and record.room_id:
                room = dao.get_room(manager.conn, record.room_id)
                # Work notes intentionally do not consume a transcript/event
                # sequence number, but their write still touches updated_at.
                # Fingerprint both so the UI receives them immediately.
                room_seq = (room.seq, room.updated_at) if room else (0, "")
                activities = tuple(
                    (item["participant_id"], item["summary"], item["expires_at"])
                    for item in dao.list_live_activities(manager.conn, record.room_id)
                )
        return cls(
            sessions=sessions,
            room_seq=room_seq,
            activities=activities,
            drafts=tuple(sorted(drafts_module.versions(manager.conn).items())),
        )


def frame(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n".encode("utf-8")


def events(workspace: Workspace, session_id: str | None, *, stop=lambda: False):
    """Yield SSE frames until the client disconnects or the cap is reached.

    Errors are reported as an event and the stream keeps going: a transient
    database lock should not tear down the user's dashboard.
    """
    broker = Broker(workspace)
    manager = SessionManager(broker)
    started = time.monotonic()
    last_beat = started
    try:
        previous = Fingerprint.take(manager, session_id)
        yield frame("ready", {"session": session_id, "interval": POLL_SECONDS})

        while not stop():
            time.sleep(POLL_SECONDS)
            now = time.monotonic()
            if now - started > MAX_STREAM_SECONDS:
                yield frame("closing", {"reason": "stream age limit; reconnect to continue"})
                return
            try:
                current = Fingerprint.take(manager, session_id)
            except Exception as exc:  # pragma: no cover - transient lock or race
                yield frame("warning", {"message": f"{type(exc).__name__}: {exc}"})
                continue

            if current != previous:
                changed = []
                if current.sessions != previous.sessions:
                    changed.append("sessions")
                if current.room_seq != previous.room_seq:
                    changed.append("conversation")
                if current.activities != previous.activities and "conversation" not in changed:
                    changed.append("conversation")
                if current.drafts != previous.drafts:
                    changed.append("drafts")
                previous = current
                last_beat = now
                yield frame("changed", {"what": changed})
            elif now - last_beat >= HEARTBEAT_SECONDS:
                last_beat = now
                yield b": keepalive\n\n"
    finally:
        broker.close()
